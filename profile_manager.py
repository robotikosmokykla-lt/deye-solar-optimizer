#!/usr/bin/env python3
"""Season/profile control for Deye Solar Optimizer v3.1.0.

The normal controller intentionally changes only MAX_SELL_POWER.  This utility is
for infrequent operating-profile changes:

  export-first       -> SELLING_FIRST
  self-consumption   -> ZERO_EXPORT_TO_CT + LOAD_FIRST

Export-first uses one official Dynamic Control order carrying BOTH the desired
work mode and the configured hard export cap.  This avoids depending on the unreliable
/config/system cache while keeping the safety limit in the same accepted order.
Self-consumption uses the same atomic work-mode+cap order and, when needed, one
separate LOAD_FIRST order.  No automatic write retry is performed.  A positive
orderId is polled until terminal status.  Live mode stops the optimizer service
while profile settings are changed, then restarts it, avoiding command-queue
collisions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from deye_api import DeyeAPIError, DeyeClient, flatten_device_latest, parse_deye_timestamp
from config_loader import DEFAULT_ENV, load_config as load_env_config, make_deye_client

SERVICE = "deye-solar-optimizer.service"
VERSION = "3.1.5"

PROFILES = {
    "export-first": {
        "work_mode": "SELLING_FIRST",
        "energy_pattern": None,
        "description": "PV/load -> grid export up to MAX_SELL_POWER -> battery remainder (to be verified on this firmware)",
    },
    "self-consumption": {
        "work_mode": "ZERO_EXPORT_TO_CT",
        "energy_pattern": "LOAD_FIRST",
        "description": "PV serves loads first, then battery; only genuine surplus is exported",
    },
}


def as_int(v: Any) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_mode(v: Any) -> Optional[str]:
    if v is None:
        return None
    text = str(v).strip().upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "SELL_FIRST": "SELLING_FIRST",
        "SELLINGFIRST": "SELLING_FIRST",
        "ZEROEXPORTTOCT": "ZERO_EXPORT_TO_CT",
        "ZERO_EXPORT_CT": "ZERO_EXPORT_TO_CT",
        "ZEROEXPORTTOLOAD": "ZERO_EXPORT_TO_LOAD",
    }
    return aliases.get(text, text)


def normalize_pattern(v: Any) -> Optional[str]:
    if v is None:
        return None
    text = str(v).strip().upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "BATTFIRST": "BATTERY_FIRST",
        "BATT_FIRST": "BATTERY_FIRST",
        "BATTERYFIRST": "BATTERY_FIRST",
        "LOADFIRST": "LOAD_FIRST",
    }
    return aliases.get(text, text)


def load_config(path: str) -> Dict[str, Any]:
    return load_env_config(path)


def make_client(cfg: Dict[str, Any]) -> DeyeClient:
    return make_deye_client(cfg)


def service_active() -> bool:
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", SERVICE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except FileNotFoundError:
        return False


def service(action: str) -> None:
    subprocess.run(["systemctl", action, SERVICE], check=True)


def write_audit(cfg: Dict[str, Any], event: str, **fields: Any) -> None:
    path = Path(cfg["logging"]["state_db"]).parent / "profile-events.jsonl"
    rec = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        _chown_deyeopt(path)
    except Exception as exc:
        print(f"WARN: could not write profile audit log: {exc}", file=sys.stderr)


def _chown_deyeopt(path: Path) -> None:
    try:
        pw = pwd.getpwnam("deyeopt")
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except Exception:
        pass


def save_profile_state(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    path = Path(cfg["logging"]["state_db"]).parent / "profile-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o640)
    _chown_deyeopt(tmp)
    os.replace(tmp, path)
    _chown_deyeopt(path)


def snapshot(client: DeyeClient, sn: str, tz: ZoneInfo) -> Dict[str, Any]:
    data = client.get_device_latest(sn)
    flat = flatten_device_latest(data)
    m = flat.get("metrics") or {}
    collected = parse_deye_timestamp(flat.get("collectionTime"), tz)
    return {
        "collection_at": collected,
        "soc": num(m.get("SOC")),
        "pv_w": num(m.get("TotalSolarPower")),
        "grid_w": num(m.get("TotalGridPower")),
        "battery_w": num(m.get("BatteryPower")),
        "load_w": num(m.get("TotalConsumptionPower")),
        "battery_voltage_v": num(m.get("BatteryVoltage")),
    }


def print_snapshot(label: str, s: Dict[str, Any]) -> None:
    grid = s.get("grid_w")
    batt = s.get("battery_w")
    export = max(0.0, -grid) if grid is not None else None
    charge = max(0.0, -batt) if batt is not None else None
    print(
        f"{label}: collection={s.get('collection_at')} SOC={s.get('soc')}% "
        f"PV={s.get('pv_w')}W load={s.get('load_w')}W grid={grid}W "
        f"export={round(export,1) if export is not None else None}W "
        f"battery={batt}W charge={round(charge,1) if charge is not None else None}W "
        f"Vbat={s.get('battery_voltage_v')}V"
    )


def wait_order(client: DeyeClient, order_id: int, timeout_s: int = 180, poll_s: int = 10) -> Tuple[str, Dict[str, Any]]:
    deadline = time.time() + timeout_s
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        time.sleep(poll_s)
        try:
            last = client.check_order_status(order_id)
        except Exception as exc:
            print(f"  status read error: {exc}")
            continue
        code = last.get("status")
        print(f"  order {order_id}: status={code} error={last.get('error')}")
        if code in (666, "666"):
            return "success", last
        if code in (500, "500"):
            return "failed", last
    return "timeout", last


def submit_once(
    client: DeyeClient,
    cfg: Dict[str, Any],
    kind: str,
    value: str,
    sn: str,
) -> Tuple[bool, Optional[int], Dict[str, Any]]:
    print(f"Submitting ONE {kind}={value} order (no write retry) ...")
    if kind == "work_mode":
        response = client.set_work_mode(sn, value)
    elif kind == "energy_pattern":
        response = client.set_energy_pattern(sn, value)
    else:
        raise ValueError(kind)
    print(json.dumps(response, indent=2, ensure_ascii=False))
    order_id = as_int(response.get("orderId"))
    connection = as_int(response.get("connectionStatus"))
    if connection == 0 or not order_id or order_id <= 0:
        write_audit(cfg, "profile_write_not_accepted", kind=kind, value=value, response=response)
        print("NOT ACCEPTED: no positive orderId / device offline. No retry sent.")
        return False, None, response
    write_audit(cfg, "profile_write_accepted", kind=kind, value=value, order_id=order_id, response=response)
    result, final = wait_order(client, order_id)
    write_audit(cfg, "profile_write_terminal", kind=kind, value=value, order_id=order_id, result=result, response=final)
    if result != "success":
        print(f"FAILED/UNCONFIRMED: {kind}={value}; terminal={result}")
        if final:
            print(json.dumps(final, indent=2, ensure_ascii=False))
        return False, order_id, final
    print(f"SUCCESS: {kind}={value} orderId={order_id}")
    return True, order_id, final


def live_status(cfg: Dict[str, Any]) -> int:
    client = make_client(cfg)
    sn = str(cfg["deye"]["inverter_sn"])
    tz = ZoneInfo(cfg["site"]["timezone"])
    print(f"Deye profile status v{VERSION}")
    print(f"SN={sn} local hard export limit={cfg['grid']['export_hard_limit_w']}W")
    try:
        syscfg = client.get_system_config(sn)
        print("System config:")
        print(
            f"  workMode={syscfg.get('systemWorkMode')}  energyPattern={syscfg.get('energyPattern')}  "
            f"maxSellPower={syscfg.get('maxSellPower')}W  maxSolarPower={syscfg.get('maxSolarPower')}W"
        )
    except Exception as exc:
        print(f"System config: unavailable ({exc})")
    try:
        battcfg = client.get_battery_config(sn)
        print("Battery config:")
        print(
            f"  maxChargeCurrent={battcfg.get('maxChargeCurrent')}A  "
            f"maxDischargeCurrent={battcfg.get('maxDischargeCurrent')}A  "
            f"lowSOC={battcfg.get('battLowCapacity')}% shutdownSOC={battcfg.get('battShutDownCapacity')}%"
        )
    except Exception as exc:
        print(f"Battery config: unavailable ({exc})")
    try:
        print_snapshot("Telemetry", snapshot(client, sn, tz))
    except Exception as exc:
        print(f"Telemetry: unavailable ({exc})")
    return 0


def load_profile_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(cfg["logging"]["state_db"]).parent / "profile-state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def submit_atomic_profile_once(
    client: DeyeClient,
    cfg: Dict[str, Any],
    *,
    sn: str,
    work_mode: str,
    hard_limit_w: int,
) -> Tuple[bool, Optional[int], Dict[str, Any]]:
    """Submit one Dynamic Control order carrying BOTH work mode and hard sell cap.

    This is intentionally used only for rare seasonal/profile changes.  It removes
    the v3.1.0 dependency on /config/system: even if the cache/read endpoint is
    unavailable, a successful accepted order cannot enter SELLING_FIRST without the
    same request also carrying maxSellPower=the locally enforced hard limit.
    """
    print(
        "Submitting ONE atomic profile order "
        f"workMode={work_mode} + maxSellPower={hard_limit_w}W (no write retry) ..."
    )
    response = client.dynamic_control(
        sn,
        max_sell_power=hard_limit_w,
        work_mode=work_mode,
    )
    print(json.dumps(response, indent=2, ensure_ascii=False))
    order_id = as_int(response.get("orderId"))
    connection = as_int(response.get("connectionStatus"))
    if connection == 0 or not order_id or order_id <= 0:
        write_audit(
            cfg,
            "profile_atomic_not_accepted",
            work_mode=work_mode,
            hard_limit_w=hard_limit_w,
            response=response,
        )
        print("NOT ACCEPTED: no positive orderId / device offline. No retry sent.")
        return False, None, response

    write_audit(
        cfg,
        "profile_atomic_accepted",
        work_mode=work_mode,
        hard_limit_w=hard_limit_w,
        order_id=order_id,
        response=response,
    )
    result, final = wait_order(client, order_id)
    write_audit(
        cfg,
        "profile_atomic_terminal",
        work_mode=work_mode,
        hard_limit_w=hard_limit_w,
        order_id=order_id,
        result=result,
        response=final,
    )
    if result != "success":
        print(f"FAILED/UNCONFIRMED: atomic profile order; terminal={result}")
        if final:
            print(json.dumps(final, indent=2, ensure_ascii=False))
        return False, order_id, final

    print(
        f"SUCCESS: workMode={work_mode} + maxSellPower={hard_limit_w}W "
        f"orderId={order_id}"
    )
    return True, order_id, final


def apply_profile(cfg: Dict[str, Any], name: str, live: bool, observe_minutes: int) -> int:
    profile = PROFILES[name]
    client = make_client(cfg)
    sn = str(cfg["deye"]["inverter_sn"])
    tz = ZoneInfo(cfg["site"]["timezone"])
    hard = int(cfg["grid"]["export_hard_limit_w"])

    print(f"Profile: {name}")
    print(f"Intent:  {profile['description']}")
    print(f"Target work mode: {profile['work_mode']}")
    if profile["energy_pattern"]:
        print(f"Target energy pattern: {profile['energy_pattern']}")
    print(f"Local hard export limit: {hard} W")

    # v3.1.0: /config/system is best-effort only.  On this installation it often
    # returns 2106002 while device/latest remains useful.  Safety no longer depends
    # on that cache because the live profile write is one atomic Dynamic Control
    # request carrying BOTH workMode and maxSellPower=hard.
    syscfg: Dict[str, Any] = {}
    current_mode: Optional[str] = None
    current_pattern: Optional[str] = None
    max_sell: Optional[int] = None
    try:
        syscfg = client.get_system_config(sn)
        current_mode = normalize_mode(syscfg.get("systemWorkMode"))
        current_pattern = normalize_pattern(syscfg.get("energyPattern"))
        max_sell = as_int(syscfg.get("maxSellPower"))
        print(
            f"Current (config/system): workMode={current_mode} "
            f"energyPattern={current_pattern} maxSellPower={max_sell}W"
        )
    except Exception as exc:
        print(f"config/system unavailable: {exc}")
        print(
            "Continuing safely: a LIVE profile order will carry "
            f"maxSellPower={hard}W in the SAME Deye order as the work-mode change."
        )

    local_state = load_profile_state(cfg)
    if local_state:
        print(
            "Local confirmed profile state: "
            f"profile={local_state.get('profile')} applied_at={local_state.get('applied_at')}"
        )

    try:
        before = snapshot(client, sn, tz)
        print_snapshot("Before", before)
    except Exception as exc:
        before = {}
        print(f"Before telemetry unavailable: {exc}")

    desired_mode = str(profile["work_mode"])
    desired_pattern = profile["energy_pattern"]

    # Avoid needless repeat writes when a fresh system read proves the requested
    # profile is already active with the correct hard cap.  If config/system is
    # unavailable, a previously confirmed local profile state is also sufficient to
    # suppress an accidental duplicate; --live can be retried after a failed order
    # because failed orders never write this local state.
    profile_already_confirmed = False
    if current_mode == desired_mode and max_sell == hard:
        if not desired_pattern or current_pattern == desired_pattern:
            profile_already_confirmed = True
    elif (
        not syscfg
        and str(local_state.get("profile", "")).strip().lower() == name
        and as_int(local_state.get("hard_limit_w")) == hard
    ):
        profile_already_confirmed = True

    if profile_already_confirmed:
        print("Requested profile is already confirmed. No write needed.")
        live = False

    if not live:
        if profile_already_confirmed:
            return 0
        print("DRY RUN ONLY. Would send:")
        print(
            f"  ONE strategy/dynamicControl order: "
            f"workMode={desired_mode}, maxSellPower={hard}"
        )
        if desired_pattern and current_pattern != desired_pattern:
            print(f"  then ONE energyPattern order: {desired_pattern}")
        print("No Deye control order was sent. Re-run with --live to apply.")
        return 0

    was_active = service_active()
    if was_active:
        if os.geteuid() != 0:
            print(
                "ABORT: optimizer service is active. Re-run with sudo so this "
                "utility can stop/restart it safely."
            )
            return 2
        print("Stopping optimizer service to avoid Deye command-queue collisions ...")
        service("stop")

    successful = False
    last_order: Optional[int] = None
    try:
        try:
            ok, order_id, _ = submit_atomic_profile_once(
                client,
                cfg,
                sn=sn,
                work_mode=desired_mode,
                hard_limit_w=hard,
            )
        except DeyeAPIError as exc:
            print(f"SUBMIT FAILED: {exc}")
            write_audit(
                cfg,
                "profile_atomic_submit_exception",
                work_mode=desired_mode,
                hard_limit_w=hard,
                error=str(exc),
            )
            ok = False
            order_id = None

        last_order = order_id
        if not ok:
            print("Profile NOT applied. No automatic write retry was made.")
            return 3

        # self-consumption also asks Deye for LOAD_FIRST.  Keep this as a second,
        # narrow, one-shot order only when required.  export-first is therefore one
        # accepted setting order total.
        if desired_pattern and current_pattern != desired_pattern:
            print("Waiting 15 s for the Deye command queue to clear ...")
            time.sleep(15)
            try:
                ok2, order2, _ = submit_once(
                    client, cfg, "energy_pattern", str(desired_pattern), sn
                )
            except DeyeAPIError as exc:
                print(f"SUBMIT FAILED: {exc}")
                write_audit(
                    cfg,
                    "profile_submit_exception",
                    kind="energy_pattern",
                    value=str(desired_pattern),
                    error=str(exc),
                )
                ok2 = False
                order2 = None
            last_order = order2 or last_order
            if not ok2:
                print(
                    "Work mode/hard cap succeeded, but energy pattern was not "
                    "confirmed. Profile NOT marked fully applied. No retry sent."
                )
                return 3

        # Persist the confirmed seasonal state BEFORE restarting the normal optimizer.
        # This prevents a restart race where the controller could run one cycle with
        # the old/unknown profile state after the Deye order already succeeded.
        now = dt.datetime.now(tz)
        try:
            save_profile_state(
                cfg,
                {
                    "profile": name,
                    "applied_at": now.isoformat(),
                    "work_mode": desired_mode,
                    "energy_pattern": desired_pattern,
                    "hard_limit_w": hard,
                    "order_id": last_order,
                    "confirmation": "Deye terminal status 666",
                },
            )
        except Exception as exc:
            print(f"WARN: could not persist local profile state: {exc}")

        successful = True
    finally:
        if was_active:
            print("Restarting optimizer service ...")
            service("start")

    if not successful:
        return 3

    # Best-effort cache confirmation only; 666 is authoritative for this utility.
    try:
        verify = client.get_system_config(sn)
        print(
            "Verified config: "
            f"workMode={normalize_mode(verify.get('systemWorkMode'))} "
            f"energyPattern={normalize_pattern(verify.get('energyPattern'))} "
            f"maxSellPower={verify.get('maxSellPower')}W"
        )
    except Exception as exc:
        print(f"Verification read unavailable (non-fatal): {exc}")

    if observe_minutes > 0:
        print(
            f"Observing telemetry for {observe_minutes} min "
            "(READ ONLY; optimizer may run normally) ..."
        )
        samples = []
        last_collection = before.get("collection_at") if before else None
        deadline = time.time() + observe_minutes * 60
        while time.time() < deadline:
            try:
                s = snapshot(client, sn, tz)
                if s.get("collection_at") and s.get("collection_at") != last_collection:
                    last_collection = s.get("collection_at")
                    samples.append(s)
                    print_snapshot("After", s)
            except Exception as exc:
                print(f"  telemetry read error: {exc}")
            time.sleep(30)
        useful = [s for s in samples if (s.get("pv_w") or 0) >= 1000]
        if useful:
            avg_export = sum(
                max(0.0, -(s.get("grid_w") or 0.0)) for s in useful
            ) / len(useful)
            avg_charge = sum(
                max(0.0, -(s.get("battery_w") or 0.0)) for s in useful
            ) / len(useful)
            avg_pv = sum(float(s.get("pv_w") or 0.0) for s in useful) / len(useful)
            print(
                f"Observation summary ({len(useful)} samples with PV>=1kW): "
                f"avg PV={avg_pv:.0f}W avg export={avg_export:.0f}W "
                f"avg battery charge={avg_charge:.0f}W"
            )
            if name == "export-first":
                print(f"Export-limit utilisation: {avg_export / hard * 100:.0f}% of {hard}W")
        else:
            print(
                "Observation summary: no new sample with PV>=1kW; "
                "cannot judge export-first behaviour."
            )

    print(f"Profile applied: {name}")
    return 0

def main() -> int:
    ap = argparse.ArgumentParser(description="Deye seasonal operating-profile manager")
    ap.add_argument("command", choices=["status", "export-first", "self-consumption"])
    ap.add_argument("--env", "--config", dest="config", default=DEFAULT_ENV, help="v3 .env file (legacy --config alias accepted)")
    ap.add_argument("--live", action="store_true", help="Actually write the requested profile. Without this flag, profile commands are dry-run.")
    ap.add_argument("--observe-minutes", type=int, default=0, help="After a successful live change, observe device/latest for N minutes (read-only).")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.command == "status":
        return live_status(cfg)
    return apply_profile(cfg, args.command, args.live, max(0, args.observe_minutes))


if __name__ == "__main__":
    raise SystemExit(main())
