#!/usr/bin/env python3
"""Read-only energy-budget planner for Deye Solar Optimizer v3.1.0."""
from __future__ import annotations

import argparse
import datetime as dt
from typing import Any, Dict
from zoneinfo import ZoneInfo

from deye_api import flatten_device_latest, parse_deye_timestamp
from energy_strategy import adaptive_night_floor_deadline, build_day_energy_plan, cooker_daily_energy_kwh, heater_daily_energy_kwh
from solar_forecast import PVArray, fetch_forecast
from state_db import StateDB
from strategy_presets import active_strategy
from config_loader import DEFAULT_ENV, load_config, make_deye_client

VERSION = "3.1.4"


def num(v: Any, default: float | None = None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only forecast-aware daytime export budget planner")
    ap.add_argument("--env", "--config", dest="config", default=DEFAULT_ENV, help="v3 .env file (legacy --config alias accepted)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    tz = ZoneInfo(cfg["site"]["timezone"])
    now = dt.datetime.now(tz)
    hard = int(cfg["grid"]["export_hard_limit_w"])

    client = make_deye_client(cfg)
    raw = client.get_device_latest(str(cfg["deye"]["inverter_sn"]))
    flat = flatten_device_latest(raw)
    metrics = flat.get("metrics") or {}
    collection_at = parse_deye_timestamp(flat.get("collectionTime"), tz)
    soc = num(metrics.get("SOC"))
    pv_now = num(metrics.get("TotalSolarPower"), 0.0) or 0.0
    load_now = num(metrics.get("TotalConsumptionPower"), 0.0) or 0.0
    grid_now = num(metrics.get("TotalGridPower"), 0.0) or 0.0
    batt_now = num(metrics.get("BatteryPower"), 0.0) or 0.0
    if soc is None or collection_at is None:
        print("ABORT: device/latest did not provide SOC + collectionTime")
        return 2
    age_min = (now - collection_at).total_seconds() / 60.0
    max_age = float(cfg.get("day_strategy", {}).get("max_telemetry_age_minutes", 15))
    if age_min > max_age:
        print(f"ABORT: telemetry is {age_min:.1f} min old; planner limit is {max_age:.0f} min")
        return 2

    arrays = [
        PVArray(str(a["name"]), float(a["kwp"]), float(a["tilt_deg"]), float(a["azimuth_deg"]))
        for a in cfg["pv"]["arrays"]
    ]
    forecasts = fetch_forecast(
        latitude=float(cfg["site"]["latitude"]),
        longitude=float(cfg["site"]["longitude"]),
        timezone=cfg["site"]["timezone"],
        arrays=arrays,
        performance_ratio=float(cfg["pv"]["performance_ratio"]),
        wake_threshold_w=float(cfg["pv"]["wake_threshold_w"]),
        useful_threshold_w=float(cfg["pv"]["useful_threshold_w"]),
        wakeup_bias_minutes=int(cfg["pv"].get("wakeup_bias_minutes", 0)),
        forecast_days=2,
    )
    day = forecasts.get(now.date())
    tomorrow = forecasts.get(now.date() + dt.timedelta(days=1))
    if not day:
        print("ABORT: no Open-Meteo forecast for today")
        return 2
    if now >= day.sunset:
        print(f"Deye daytime plan v{VERSION} -- sunset has passed; no daytime export budget remains.")
        return 0

    db = StateDB(cfg["logging"]["state_db"])
    try:
        plan = build_day_energy_plan(cfg, db, day, now, soc, tomorrow)
        floor_deadline, morning_bias, morning_bias_source = adaptive_night_floor_deadline(cfg, db, day, now)
    finally:
        db.close()

    actual_export_w = max(0.0, -grid_now)
    actual_charge_w = max(0.0, -batt_now)
    export_headroom_w = max(0.0, float(hard) - actual_export_w)
    wh = cfg.get("water_heater", {})
    policy = active_strategy(cfg)

    if plan.recommended_export_w >= hard:
        recommendation = "EXPORT_FIRST_FULL"
        why = f"safe energy budget supports the full {hard}W cap with {plan.full_export_margin_kwh:+.2f}kWh margin"
    elif plan.recommended_export_w > 0:
        recommendation = "EXPORT_FIRST_LIMITED"
        why = f"spread today's safe export budget at about {plan.recommended_export_w}W average"
    else:
        recommendation = "PROTECT_BATTERY"
        why = "safe forecast budget is needed for house/heater/losses and battery target"

    print(f"Deye daytime plan v{VERSION} -- {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Strategy:   {policy.tag} -- {policy.description}")
    print(f"Telemetry: age={age_min:.1f}min SOC={soc:.1f}% PV={pv_now:.0f}W load={load_now:.0f}W grid={grid_now:.0f}W battery={batt_now:.0f}W")
    print(f"Now:       direct export={actual_export_w:.0f}/{hard}W battery charge={actual_charge_w:.0f}W unused export headroom={export_headroom_w:.0f}W")
    if plan.morning_desired_soc_pct is not None:
        print(
            f"Morning:   day budget starts={plan.planning_start_at} desired reserve={plan.morning_desired_soc_pct:.1f}% "
            f"projected={plan.morning_projected_soc_pct:.1f}%"
        )
    else:
        print(f"Morning:   day budget starts now={plan.planning_start_at}; sustained-PV handoff={floor_deadline.isoformat()} bias={morning_bias:+.0f}min ({morning_bias_source})")
    print(
        f"Battery:   planning target={plan.target_soc_pct:.1f}% reason={plan.target_reason} maintenance_due={str(plan.maintenance_due).lower()} "
        f"last_full={plan.last_full_at or 'unknown'}"
    )
    print(
        f"Forecast:  raw from planning start={plan.raw_remaining_pv_kwh:.2f}kWh "
        f"bias-corrected={plan.bias_corrected_pv_kwh:.2f}kWh safe={plan.safe_remaining_pv_kwh:.2f}kWh "
        f"factor={plan.safe_forecast_factor:.2f} base={plan.base_safe_forecast_factor:.2f} ({plan.forecast_factor_source})"
    )
    print(f"Intraday:  bias={plan.intraday_bias:.3f} ({plan.intraday_bias_source})")
    print(f"Probabil.: {plan.forecast_distribution_source} {plan.forecast_distribution or '{}'}")
    print(
        f"Loads:     learned house={plan.house_load_w:.0f}W ({plan.house_load_source}) => {plan.house_energy_kwh:.2f}kWh; "
        f"inverter/system={plan.system_overhead_kwh:.2f}kWh"
    )
    if bool(wh.get("enabled", False)):
        print(
            f"Heater:    scheduled={wh.get('scheduled_time','10:00')} nominal daily={heater_daily_energy_kwh(cfg):.2f}kWh "
            f"remaining={plan.water_heater_kwh:.2f}kWh status={plan.water_heater_status}"
        )
    cooker = cfg.get("cooker", {})
    if bool(cooker.get("enabled", False)):
        print(
            f"Cooker:    scheduled={cooker.get('scheduled_time','18:00')} nominal daily={cooker_daily_energy_kwh(cfg):.2f}kWh "
            f"remaining={plan.cooker_kwh:.2f}kWh status={plan.cooker_status}"
        )
    print(
        f"Target:    planning SOC={plan.planning_soc_pct:.1f}%; battery stored need={plan.battery_stored_kwh_needed:.2f}kWh; PV input need={plan.battery_input_kwh_needed:.2f}kWh; "
        f"reserve={plan.reserve_kwh:.2f}kWh ({plan.reserve_source})"
    )
    print(
        f"EndOfDay:  target SOC={plan.end_of_day_target_soc_pct:.1f}% reason={plan.end_of_day_target_reason}; "
        f"night need={plan.night_energy_need_kwh:.2f}kWh over {plan.night_hours:.1f}h (reserve {plan.reserve_kwh:.2f}kWh included)"
    )
    print(
        f"Budget:    PV surplus={plan.pv_surplus_kwh:+.2f}kWh + stored surplus={plan.stored_surplus_kwh:.2f}kWh "
        f"=> export energy={plan.export_energy_budget_kwh:.2f}kWh over {plan.hours_to_sunset:.2f}h; "
        f"sustains {hard}W for {plan.cap_sustain_hours:.2f}h; full-{hard}W margin={plan.full_export_margin_kwh:+.2f}kWh"
    )
    if plan.surplus_above_cap_kwh > 0 or plan.deficit_covered_by_above_cap_kwh > 0:
        print(
            f"AboveCap:  {plan.surplus_above_cap_kwh:.2f}kWh arrives faster than the {hard}W cap can carry; "
            f"{plan.deficit_covered_by_above_cap_kwh:.2f}kWh of the charge deficit is covered by it"
        )
    print(f"Floor:     staleness floor={plan.staleness_floor_w}W ({plan.staleness_floor_reason})")
    print(f"Allocation: {plan.allocation_mode}")
    print(f"RECOMMENDED MAX_SELL_POWER: {plan.recommended_export_w} W")
    print(f"RECOMMENDATION: {recommendation} -- {why}")
    print("NOTE: 96% is a planning target, not a hard charge ceiling. With >1kW true surplus, Deye may still charge above it; v3.1.0 does not issue unproven battery-current/TOU writes.")
    print("READ ONLY: no inverter setting was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
