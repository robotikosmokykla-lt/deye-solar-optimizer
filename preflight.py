#!/usr/bin/env python3
"""Read-only commissioning check. Never sends a setting update."""
from __future__ import annotations

import argparse
import datetime as dt
from zoneinfo import ZoneInfo

from deye_api import flatten_device_latest, parse_deye_timestamp
from solar_forecast import PVArray, fetch_forecast
from energy_strategy import night_floor_deadline
from config_loader import DEFAULT_ENV, load_config, make_deye_client


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", "--config", dest="config", default=DEFAULT_ENV)
    args = ap.parse_args()
    cfg = load_config(args.config)
    tz = ZoneInfo(cfg["site"]["timezone"])
    now = dt.datetime.now(tz)
    sn = str(cfg["deye"]["inverter_sn"])

    print(f"Preflight v3.1.0 at {now.isoformat()}")
    print("1) Configuration: OK")
    print(f"   hard export limit = {cfg['grid']['export_hard_limit_w']} W")
    print(f"   battery = {cfg['battery']['effective_kwh']} kWh, floor = {cfg['battery']['soc_floor_pct']}%")
    print(f"   dry_run = {cfg.get('control', {}).get('dry_run', True)}")
    print(f"   write_api = {cfg.get('control', {}).get('write_api', 'power_update')}")

    client = make_deye_client(cfg)
    client.get_token()
    print("2) Deye authentication: OK")

    dev = client.get_device_latest(sn)
    flat = flatten_device_latest(dev)
    collection = parse_deye_timestamp(flat.get("collectionTime"), tz)
    m = flat["metrics"]
    print("3) Deye device/latest: OK (primary telemetry)")
    print(
        f"   collection={collection} deviceState={flat.get('deviceState')} SOC={m.get('SOC')}% "
        f"battery={m.get('BatteryPower')}W grid={m.get('TotalGridPower')}W "
        f"load={m.get('TotalConsumptionPower')}W PV={m.get('TotalSolarPower')}W"
    )

    # v3.1.0 deliberately does NOT test Dynamic Control READ here. Although it
    # does not change a setting, Deye implements it as an asynchronous device
    # command with an orderId and it can occupy the command queue (2104004).
    print("4) Control path: NOT PROBED (intentional; preflight is passive in v3.1.0)")
    print("   Live mode uses the actual MAX_SELL_POWER submission as the online probe.")

    # station/latest is diagnostic only in v3.1.0.
    try:
        st = client.get_station_latest(int(cfg["deye"]["station_id"]))
        station_ts = parse_deye_timestamp(st.get("lastUpdateTime"), tz)
        print(f"5) station/latest: OK (diagnostic only) timestamp={station_ts} SOC={st.get('batterySOC')}")
    except Exception as exc:
        print(f"5) station/latest: WARN (not used for control): {exc}")

    arrays = [
        PVArray(a["name"], float(a["kwp"]), float(a["tilt_deg"]), float(a["azimuth_deg"]))
        for a in cfg["pv"]["arrays"]
    ]
    fc = fetch_forecast(
        float(cfg["site"]["latitude"]),
        float(cfg["site"]["longitude"]),
        cfg["site"]["timezone"],
        arrays,
        float(cfg["pv"]["performance_ratio"]),
        float(cfg["pv"]["wake_threshold_w"]),
        float(cfg["pv"]["useful_threshold_w"]),
        int(cfg["pv"].get("wakeup_bias_minutes", 0)),
        3,
    )
    print("6) Open-Meteo: OK")
    for day in sorted(fc):
        f = fc[day]
        control_wake = night_floor_deadline(cfg, f)
        print(
            f"   {day}: sunrise={f.sunrise.strftime('%H:%M')} floorDeadline={control_wake.strftime('%H:%M')} "
            f"forecastWake={f.pv_wakeup.strftime('%H:%M')} useful={f.useful_pv_start.strftime('%H:%M')} "
            f"sunset={f.sunset.strftime('%H:%M')} expected={f.expected_kwh:.2f} kWh"
        )

    print("\nPRE-FLIGHT PASSED. No inverter setting was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
