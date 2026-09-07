#!/usr/bin/env python3
"""Human-readable health/status report for Deye Solar Optimizer v3.1.0."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path
from strategy_presets import active_strategy
from config_loader import DEFAULT_ENV, load_config
from zoneinfo import ZoneInfo


def ago(now: dt.datetime, text: str | None) -> str:
    if not text:
        return "n/a"
    try:
        t = dt.datetime.fromisoformat(text)
        s = max(0, int((now - t).total_seconds()))
    except Exception:
        return "invalid"
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s/60:.0f}min"
    return f"{s/3600:.1f}h"


def fetch_one(conn: sqlite3.Connection, sql: str, args=()):
    return conn.execute(sql, args).fetchone()


def kv_get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def report(config_path: str) -> None:
    cfg = load_config(config_path)
    tz = ZoneInfo(cfg["site"]["timezone"])
    now = dt.datetime.now(tz)
    policy = active_strategy(cfg)
    db_path = cfg["logging"]["state_db"]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        print(f"Deye Solar Optimizer v3.1.0 -- {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print("Health: NOT INITIALIZED")
        print(f"State DB does not exist yet: {db_path}")
        return
    conn.row_factory = sqlite3.Row
    required = {"telemetry", "forecasts", "decisions", "writes", "api_events", "kv"}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    missing = sorted(required - tables)
    if missing:
        conn.close()
        print(f"Deye Solar Optimizer v3.1.0 -- {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print("Health: DB SCHEMA NOT INITIALIZED")
        print(f"Missing tables: {', '.join(missing)}")
        return

    today = now.date().isoformat()
    tomorrow = (now.date() + dt.timedelta(days=1)).isoformat()
    tel = fetch_one(conn, "SELECT * FROM telemetry ORDER BY id DESC LIMIT 1")
    dec = fetch_one(conn, "SELECT * FROM decisions ORDER BY id DESC LIMIT 1")
    wr = fetch_one(conn, "SELECT * FROM writes ORDER BY id DESC LIMIT 1")
    pending = fetch_one(conn, "SELECT * FROM writes WHERE accepted=1 AND status='pending' ORDER BY id DESC LIMIT 1")
    fc = fetch_one(conn, "SELECT * FROM forecasts WHERE target_date=? ORDER BY id DESC LIMIT 1", (today,))
    fc_tomorrow = fetch_one(conn, "SELECT * FROM forecasts WHERE target_date=? ORDER BY id DESC LIMIT 1", (tomorrow,))
    if not fc:
        fc = fetch_one(conn, "SELECT * FROM forecasts ORDER BY id DESC LIMIT 1")

    current_soc = kv_get(conn, "current_soc")
    current_soc_raw = kv_get(conn, "current_soc_raw")
    soc_confidence = kv_get(conn, "soc_confidence", "n/a")
    telemetry_age = kv_get(conn, "telemetry_age_minutes")
    device_state = kv_get(conn, "device_state")
    current_setting = kv_get(conn, "last_known_setting_w")
    control_state = kv_get(conn, "control_path_state", "UNKNOWN")
    control_last_attempt = kv_get(conn, "control_path_last_attempt_at")
    control_last_online = kv_get(conn, "control_path_last_online_at")
    control_detail = kv_get(conn, "control_path_last_detail", "")
    active_order_id = kv_get(conn, "active_order_id")
    uncertain_until = kv_get(conn, "uncertain_write_guard_until")
    uncertain_detail = kv_get(conn, "uncertain_write_detail", "")
    telemetry_health = kv_get(conn, "telemetry_health_state", "UNKNOWN")
    telemetry_stall_started = kv_get(conn, "telemetry_stall_started_at")
    telemetry_last_recovered = kv_get(conn, "telemetry_last_recovered_at")
    telemetry_last_stall_duration = kv_get(conn, "telemetry_last_stall_duration_min")
    day_energy_plan = kv_get(conn, "day_energy_plan", {}) or {}
    current_day_target = kv_get(conn, "current_day_target_soc")
    current_night_target = kv_get(conn, "current_night_target_soc")
    morning_soc_plan = kv_get(conn, "morning_soc_plan", {}) or {}
    maintenance_due = kv_get(conn, "maintenance_due")
    last_full_balance = kv_get(conn, "last_full_balance_at")
    profile_state = {}
    profile_state_path = Path(db_path).parent / "profile-state.json"
    try:
        profile_state = json.loads(profile_state_path.read_text(encoding="utf-8"))
    except Exception:
        profile_state = {}
    operating_profile = profile_state.get("profile", "not-set")
    profile_applied_at = profile_state.get("applied_at")
    profile_work_mode = profile_state.get("work_mode")
    profile_energy_pattern = profile_state.get("energy_pattern")
    profile_order_id = profile_state.get("order_id")

    start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    accepted_today = fetch_one(conn, "SELECT COUNT(*) c FROM writes WHERE ts>=? AND accepted=1", (start,))["c"]
    dry_today = fetch_one(conn, "SELECT COUNT(*) c FROM writes WHERE ts>=? AND status='dry-run'", (start,))["c"]
    successes = fetch_one(conn, "SELECT COUNT(*) c FROM writes WHERE accepted=1 AND status='success'")["c"]
    failures = fetch_one(conn, "SELECT COUNT(*) c FROM writes WHERE accepted=1 AND status='failed'")["c"]
    pendings = fetch_one(conn, "SELECT COUNT(*) c FROM writes WHERE accepted=1 AND status='pending'")["c"]

    since6 = (now - dt.timedelta(hours=6)).isoformat()
    errors = fetch_one(conn, "SELECT COUNT(*) c FROM api_events WHERE ts>=? AND ok=0", (since6,))["c"]
    error_groups = conn.execute(
        "SELECT operation,COUNT(*) c FROM api_events WHERE ts>=? AND ok=0 GROUP BY operation ORDER BY c DESC",
        (since6,),
    ).fetchall()

    score = 100
    flags: list[str] = []
    if tel:
        collection_age = None
        try:
            collection_age = (now - dt.datetime.fromisoformat(tel["logger_at"])).total_seconds() / 60.0
        except Exception:
            pass
        cloud_offline_min = float(cfg.get("telemetry_health", {}).get("cloud_offline_minutes", 20))
        if collection_age is not None and collection_age > cloud_offline_min:
            score -= 25
            flags.append(f"device telemetry {collection_age:.0f}min old")
    else:
        score -= 30
        flags.append("no device telemetry")
    if not fc:
        score -= 20
        flags.append("no forecast")
    if errors:
        score -= min(30, int(errors) * 3)
        flags.append(f"{errors} real API errors/6h")
    if pending:
        try:
            p_age = (now - dt.datetime.fromisoformat(pending["ts"])).total_seconds() / 60.0
            if p_age > 30:
                score -= 10
                flags.append(f"accepted order pending {p_age:.0f}min")
        except Exception:
            pass
    if uncertain_until:
        try:
            u = dt.datetime.fromisoformat(uncertain_until)
            if now < u:
                score -= 15
                flags.append(f"uncertain write guard until {u.strftime('%H:%M')}")
        except Exception:
            pass
    score = max(0, score)
    grade = "GOOD" if score >= 90 else "WARN" if score >= 70 else "BAD"

    print(f"Deye Solar Optimizer v3.1.0 -- {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Health: {grade}  score={score}/100" + (f"  flags: {', '.join(flags)}" if flags else ""))
    print(f"Strategy:  {policy.tag} -- {policy.description}")
    print("-" * 86)

    if tel:
        print(
            f"Telemetry: source={tel['source'] or 'legacy'} collection={tel['logger_at']} age={ago(now, tel['logger_at'])} "
            f"deviceState={tel['device_state'] if 'device_state' in tel.keys() else 'n/a'}"
        )
        print(
            f"           SOC raw={current_soc_raw if current_soc_raw is not None else tel['soc']}% "
            f"control={round(float(current_soc),2) if current_soc is not None else 'n/a'}% "
            f"confidence={soc_confidence} age_model={round(float(telemetry_age),1) if telemetry_age is not None else 'n/a'}min"
        )
        print(
            f"           PV={tel['generation_power']}W grid={tel['grid_power']}W battery={tel['battery_power']}W "
            f"load={tel['consumption_power']}W UPS={tel['ups_power'] if 'ups_power' in tel.keys() else None}W "
            f"Vbat={tel['battery_voltage'] if 'battery_voltage' in tel.keys() else None}V"
        )
        print(
            f"Cloud:     telemetry_health={telemetry_health} stall_started={telemetry_stall_started or 'none'} "
            f"last_recovered={telemetry_last_recovered or 'n/a'} last_stall={round(float(telemetry_last_stall_duration),1) if telemetry_last_stall_duration is not None else 'n/a'}min"
        )
        print(
            f"Energy:    today PV={tel['daily_production_kwh'] if 'daily_production_kwh' in tel.keys() else None}kWh "
            f"load={tel['daily_consumption_kwh'] if 'daily_consumption_kwh' in tel.keys() else None}kWh; "
            f"totals buy={tel['total_buy_kwh'] if 'total_buy_kwh' in tel.keys() else None} sell={tel['total_sell_kwh'] if 'total_sell_kwh' in tel.keys() else None} "
            f"charge={tel['total_charge_kwh'] if 'total_charge_kwh' in tel.keys() else None} discharge={tel['total_discharge_kwh'] if 'total_discharge_kwh' in tel.keys() else None} kWh"
        )
    else:
        print("Telemetry: none")

    if fc:
        ns = cfg.get("night_strategy", {})
        print(f"Forecast:  date={fc['target_date']} fetched={fc['fetched_at']} age={ago(now, fc['fetched_at'])}")
        print(
            f"           sunrise={fc['sunrise']} forecast_wake={fc['pv_wakeup']} useful={fc['useful_pv_start']} "
            f"sunset={fc['sunset']} expected={fc['expected_kwh']:.2f}kWh"
        )
        print(
            f"NightRule: handoff near sustained PV>={ns.get('morning_surplus_threshold_w',350)}W for "
            f"{ns.get('sustained_minutes',30)}min, lead={ns.get('floor_lead_minutes',10)}min; current target_time={kv_get(conn,'target_time') or 'n/a'}"
        )
        if morning_soc_plan:
            print(
                f"MorningSOC: desired={morning_soc_plan.get('desired_soc_pct')}% projected={morning_soc_plan.get('projected_soc_pct')}% "
                f"export={morning_soc_plan.get('export_w')}W reason={morning_soc_plan.get('reason')}"
            )
        if fc_tomorrow:
            print(
                f"Tomorrow:  forecast_wake={fc_tomorrow['pv_wakeup']} useful={fc_tomorrow['useful_pv_start']} "
                f"expected={fc_tomorrow['expected_kwh']:.2f}kWh sunset={fc_tomorrow['sunset']}"
            )
    else:
        print("Forecast: none")

    if dec:
        print(
            f"Decision:  phase={dec['phase']} SOC={dec['current_soc']} target={dec['target_soc']} "
            f"target_time={dec['target_time']}"
        )
        print(
            f"           recommended={dec['recommended_w']}W current={current_setting if current_setting is not None else dec['current_setting_w']}W "
            f"action={dec['action']} reason={dec['reason']}"
        )

    print(
        f"Battery:   night_target={current_night_target if current_night_target is not None else 'n/a'}% "
        f"day_target={current_day_target if current_day_target is not None else cfg.get('battery',{}).get('day_target_soc_pct',96)}% "
        f"maintenance_due={maintenance_due if maintenance_due is not None else 'n/a'} last_full={last_full_balance or 'history/unknown'}"
    )
    if day_energy_plan:
        print(
            f"DayBudget: strategy={day_energy_plan.get('strategy_tag')} start={day_energy_plan.get('planning_start_at')} "
            f"planningSOC={day_energy_plan.get('planning_soc_pct')}% safePV={day_energy_plan.get('safe_remaining_pv_kwh')}kWh "
            f"factor={day_energy_plan.get('safe_forecast_factor')} house={day_energy_plan.get('house_load_w')}W "
            f"heater_remaining={day_energy_plan.get('water_heater_kwh')}kWh cooker_remaining={day_energy_plan.get('cooker_kwh',0)}kWh "
            f"scheduled_remaining={day_energy_plan.get('scheduled_loads_kwh',day_energy_plan.get('water_heater_kwh'))}kWh "
            f"export_budget={day_energy_plan.get('export_energy_budget_kwh')}kWh recommended={day_energy_plan.get('recommended_export_w')}W"
        )
    print(
        f"Control:   state={control_state} last_attempt={control_last_attempt or 'none'} "
        f"last_online={control_last_online or 'never'} current_setting={current_setting}W"
    )
    if control_detail:
        print(f"           detail={str(control_detail)[:180]}")
    if uncertain_until:
        print(f"Guard:     uncertain-submit until={uncertain_until}")
        if uncertain_detail:
            print(f"           detail={str(uncertain_detail)[:180]}")
    print(
        f"Profile:   last_applied={operating_profile} at={profile_applied_at or 'never'} "
        f"work_mode={profile_work_mode or 'unknown'} energy_pattern={profile_energy_pattern or 'unchanged/unknown'} "
        f"order_id={profile_order_id or 'n/a'}"
    )

    if pending:
        print(
            f"Order:     ACTIVE id={pending['order_id']} target={pending['requested_w']}W status={pending['status']} "
            f"submitted={pending['ts']} age={ago(now, pending['ts'])}"
        )
    elif active_order_id:
        print(f"Order:     state key says active id={active_order_id}, but no pending DB row found")
    else:
        print("Order:     none active")

    if wr:
        print(
            f"Last write:{' ' if len(str(wr['ts'])) < 1 else ' '} {wr['ts']} {wr['previous_w']} -> {wr['requested_w']}W "
            f"status={wr['status']} accepted={wr['accepted']} reason={wr['reason']}"
        )
    else:
        print("Last write: none")
    normal_max = int(cfg.get("control", {}).get("max_successful_writes_per_day", cfg.get("control", {}).get("max_writes_per_day", 4)))
    bonus_max = int(cfg.get("control", {}).get("max_successful_writes_with_bonus", 5))
    max_submissions = int(cfg.get("control", {}).get("max_order_submissions_per_day", 8))
    successful_today = fetch_one(conn, "SELECT COUNT(*) c FROM writes WHERE ts>=? AND accepted=1 AND status='success'", (start,))["c"]
    submissions_today = accepted_today
    print(
        f"Writes:    successful today={successful_today}/{normal_max} (+bonus to {bonus_max} for large delta); "
        f"submissions={submissions_today}/{max_submissions}; all success={successes} pending={pendings} failed={failures}; dry-run today={dry_today}"
    )
    if error_groups:
        grouped = ", ".join(f"{r['operation']}={r['c']}" for r in error_groups)
        print(f"API:       real errors last 6h={errors} ({grouped})")
    else:
        print("API:       real errors last 6h=0; explicit OFFLINE/BUSY write rejections are not counted as errors")
    print(
        f"Config:    dry_run={str(cfg.get('control', {}).get('dry_run', True)).lower()} "
        f"control=direct_write_only write_api={cfg.get('control', {}).get('write_api', 'power_update')} "
        f"retry={cfg.get('control', {}).get('offline_retry_seconds', 60)}s hard_limit={cfg['grid']['export_hard_limit_w']}W"
    )
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", "--config", dest="config", default=DEFAULT_ENV, help="v3 .env file (legacy --config alias accepted)")
    ap.add_argument("--watch", type=int, default=0, metavar="SECONDS")
    args = ap.parse_args()
    while True:
        report(args.config)
        if args.watch <= 0:
            return
        print("\n" + "=" * 86 + "\n")
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
