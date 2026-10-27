#!/usr/bin/env python3
"""Apply infrequent seasonal reserves using native inverter settings.

Run without --apply for a local preview. The daily timer does no API work when
the desired profile is already confirmed. Manual inverter edits are therefore
left alone until the next season/configuration change (or --force).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import time

from config_loader import DEFAULT_ENV, load_config, make_deye_client
from profile_manager import _chown_deyeopt, service, service_active, write_audit
from seasonal_policy import reserve_request, request_matches
from state_db import StateDB


def fingerprint(cfg):
    return json.dumps({
        "season": cfg["season"], "day_soc": cfg["battery"]["soc_floor_pct"],
        "night_soc": cfg["night_strategy"]["morning_soc_pct"],
        "night_export_w": cfg["night_strategy"]["max_export_w"],
        "house_mode": cfg.get('house', {}).get('mode') if cfg.get('house', {}).get('enabled') else None,
    }, sort_keys=True)


def read_settings(client, sn):
    response = client.dynamic_control_read(sn)
    order = int(response.get("orderId") or 0)
    if order <= 0:
        raise RuntimeError("Device did not accept the settings read")
    for _ in range(15):
        time.sleep(2)
        result = client.dynamic_control_read_result(order)
        if result.get("timeUseSettingItems"):
            return result
    raise RuntimeError("Settings read is still pending; no setting was written")


def wait_terminal(client, order):
    for _ in range(36):
        time.sleep(5)
        result = client.check_order_status(order)
        if str(result.get("status")) in {"666", "500"}:
            return result
    raise RuntimeError("Seasonal order still pending; its ID is saved to prevent duplicate writes")


def apply_native(client, cfg, state, save):
    sn = cfg["deye"]["inverter_sn"]
    # Resolve an accepted or ambiguous earlier submission before doing anything else.
    if state.get("pending_order_id"):
        result = wait_terminal(client, state["pending_order_id"])
        state["last_result"] = result
        state.pop("pending_order_id")
        state["retry_after"] = time.time() + 7200
        save(state)
    if time.time() < float(state.get("retry_after", 0)):
        raise RuntimeError("Seasonal write backoff is active; no new command sent")
    current = read_settings(client, sn)
    desired = reserve_request(cfg, current)
    state["before"] = current
    state["requested"] = desired
    save(state)
    if not request_matches(current, desired):
        # Persist before submitting: a timeout can hide an accepted command.
        state["retry_after"] = time.time() + 7200
        save(state)
        response = client.set_reserve_schedule(sn, **desired)
        order = int(response.get("orderId") or 0)
        if order <= 0:
            raise RuntimeError("Seasonal order was not accepted; no immediate retry")
        state["pending_order_id"] = order
        save(state)
        write_audit(cfg, "seasonal_order_submitted", order_id=order, profile=cfg["season"]["active"], requested=desired)
        result = wait_terminal(client, order)
        state["last_result"] = result
        state.pop("pending_order_id")
        save(state)
        if str(result.get("status")) != "666":
            raise RuntimeError(f"Seasonal order failed: {result.get('error')}; retry held for two hours")
        current = read_settings(client, sn)
        if not request_matches(current, desired):
            raise RuntimeError("Inverter readback does not match the requested reserve settings")
    state.update(applied=fingerprint(cfg), confirmed_at=dt.datetime.now(dt.timezone.utc).isoformat(), verified=current)
    state.pop("retry_after", None)
    save(state)
    write_audit(cfg, "seasonal_profile_verified", profile=cfg["season"]["active"], requested=desired)


def check_control_idle(cfg):
    db = StateDB(cfg["logging"]["state_db"], readonly=True)
    try:
        if db.last_pending_write():
            raise RuntimeError("Optimizer has a pending order; seasonal update deferred")
        temperature = db.get("charge_temperature_state", {}) or {}
        if temperature.get("pending"):
            raise RuntimeError("Temperature control has a pending order; seasonal update deferred")
        from charge_temperature import blocks_command_queue
        if blocks_command_queue(temperature, dt.datetime.now(dt.timezone.utc)):
            raise RuntimeError("Temperature control has an unresolved command guard; seasonal update deferred")
        guard = db.get("uncertain_write_guard_until")
        if guard and dt.datetime.now(dt.timezone.utc) < dt.datetime.fromisoformat(guard):
            raise RuntimeError("Optimizer has an uncertain command; seasonal update deferred")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default=DEFAULT_ENV)
    ap.add_argument("--apply", action="store_true", help="Apply and verify native reserve settings")
    ap.add_argument("--force", action="store_true", help="Recheck this profile even if previously confirmed")
    args = ap.parse_args()
    cfg = load_config(args.env)
    if cfg["season"]["active"] == "off" and not cfg.get('house', {}).get('enabled'):
        print("Season defaults disabled (SEASON_MODE=off).")
        return 0
    print(f"Season: {cfg['season']['active']}; overnight {cfg['night_strategy']['morning_soc_pct']:g}%; "
          f"day {cfg['battery']['soc_floor_pct']:g}%; grid charge {cfg['season']['grid_charge_ampere']}A; "
          f"night export <= {cfg['night_strategy']['max_export_w']}W", flush=True)
    if not args.apply or cfg["control"]["dry_run"]:
        if cfg.get('house', {}).get('enabled'):
            print(f"House mode: {cfg['house']['mode']}; storage target {cfg['house']['storage_soc_pct']}%")
        print("Preview only. No API call or service restart.")
        return 0
    directory = Path(cfg["logging"]["state_db"]).parent
    path = directory / "season-state.json"

    def save(state):
        temp = path.with_suffix(".tmp")
        with temp.open("w") as output:
            output.write(json.dumps(state, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        temp.chmod(0o640)
        _chown_deyeopt(temp)
        os.replace(temp, path)

    with (directory / "season.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(path.read_text()) if path.exists() else {}
        house_state = None
        if cfg.get('house', {}).get('enabled'):
            from house_policy import read_state, fetch_weather, battery_history, build_plan, apply_plan, solar_opportunity
            house_state = read_state(cfg)
            previous = house_state.get('plan', {})
            if previous.get('mode') != cfg['house']['mode']:
                previous = {}
            now = dt.datetime.now(dt.timezone.utc)
            weather = house_state.get('weather')
            if cfg['house']['mode'] == 'occupied':
                expired = not weather or (now - dt.datetime.fromisoformat(weather['fetched_at'])).total_seconds() > 3600
                if expired:
                    try:
                        weather = fetch_weather(cfg['site'], now, cfg['house']['weather_station'])
                        house_state['weather'] = weather
                        house_state.pop('weather_error', None)
                    except Exception as exc:
                        house_state['weather_error'] = str(exc)[:300]
            db = StateDB(cfg['logging']['state_db'], readonly=True)
            try:
                history = battery_history(db, now)
                temperature = history[-1][1] if history and 0 <= (now-history[-1][0]).total_seconds() <= 600 else None
                solar = solar_opportunity(cfg, db, now, temperature)
                plan = build_plan(cfg, previous, history, now, weather, solar)
            finally:
                db.close()
            apply_plan(cfg, plan)
            house_state['plan'] = plan
            print('House plan: ' + json.dumps(plan), flush=True)
            if plan.get('alerts'):
                print('WARNING: ' + ', '.join(plan['alerts']), flush=True)

        def save_house():
            if house_state is not None:
                from house_policy import state_path
                target = state_path(cfg)
                temp = target.with_suffix('.tmp')
                with temp.open('w') as output:
                    output.write(json.dumps(house_state, indent=2) + '\n')
                    output.flush()
                    os.fsync(output.fileno())
                temp.chmod(0o640)
                _chown_deyeopt(temp)
                os.replace(temp, target)

        if (state.get("applied") == fingerprint(cfg) and not state.get("pending_order_id")
                and not state.get("retry_after") and not args.force):
            save_house()
            print("Profile already confirmed; no API call or service restart.")
            return 0
        running = service_active()
        if running:
            service("stop")
        try:
            check_control_idle(cfg)
            apply_native(make_deye_client(cfg), cfg, state, save)
            save_house()
            print("Native reserve schedule verified.")
        finally:
            if running:
                service("start")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Season update deferred: {exc}", flush=True)
        raise SystemExit(1)
