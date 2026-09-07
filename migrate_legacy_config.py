#!/usr/bin/env python3
"""Convert a v2 config.toml + credential files to a single v3 deye.env."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tomllib


def j(v) -> str:
    return json.dumps(str(v), ensure_ascii=False)


def b(v) -> str:
    return "true" if bool(v) else "false"


def read_secret(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--credentials-dir", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    with open(args.config, "rb") as fh:
        c = tomllib.load(fh)
    cred = Path(args.credentials_dir)
    d = c.get("deye", {})
    pv = c.get("pv", {})
    arrays = pv.get("arrays", [])
    wh = c.get("water_heater", {})
    cooker = c.get("cooker", {})
    ctl = c.get("control", {})
    ds = c.get("day_strategy", {})
    lf = c.get("load_forecast", {})
    fu = c.get("forecast_uncertainty", {})
    th = c.get("telemetry_health", {})
    ns = c.get("night_strategy", {})
    maint = c.get("maintenance", {})

    # Preserve legacy appliance timing by default. Site-specific changes can be supplied
    # to upgrade.sh with --water-heater-time, keeping this migrator reusable on GitHub.
    wh_time = str(wh.get("scheduled_time", "10:00"))

    bundle_dir = "/srv/ollama/Projects/deye" if Path("/srv/ollama/Projects/deye").exists() else "/var/lib/deye-solar-optimizer/exports"

    lines = [
        "# Deye Solar Optimizer v3.1.0 - migrated from legacy config.toml",
        f"DEYE_APP_ID={j(read_secret(cred/'app-id.txt'))}",
        f"DEYE_APP_SECRET={j(read_secret(cred/'app-secret.txt'))}",
        f"DEYE_LOGIN={j(read_secret(cred/'login.txt'))}",
        f"DEYE_PASSWORD={j(read_secret(cred/'login-pass.txt'))}",
        f"DEYE_BASE_URL={j(d.get('base_url','https://eu1-developer.deyecloud.com/v1.0'))}",
        f"DEYE_STATION_ID={int(d.get('station_id',0))}",
        f"DEYE_INVERTER_SN={j(d.get('inverter_sn',''))}",
        "",
        f"SITE_LATITUDE={float(c.get('site',{}).get('latitude',0.0))}",
        f"SITE_LONGITUDE={float(c.get('site',{}).get('longitude',0.0))}",
        f"SITE_TIMEZONE={j(c.get('site',{}).get('timezone','Etc/UTC'))}",
        f"STRATEGY_ACTIVE={j(c.get('strategy',{}).get('active','conservative'))}",
        "",
        f"BATTERY_EFFECTIVE_KWH={float(c.get('battery',{}).get('effective_kwh',15.0))}",
        f"BATTERY_SOC_FLOOR_PCT={float(c.get('battery',{}).get('soc_floor_pct',15.0))}",
        f"BATTERY_DAY_TARGET_SOC_PCT={float(c.get('battery',{}).get('day_target_soc_pct',96.0))}",
        f"BATTERY_MAINTENANCE_TARGET_SOC_PCT={float(maint.get('target_soc_pct',100.0))}",
        f"BATTERY_MAINTENANCE_INTERVAL_DAYS={int(maint.get('interval_days',30))}",
        f"BATTERY_FULL_SOC_THRESHOLD_PCT={float(maint.get('full_soc_threshold_pct',99.5))}",
        "",
        f"GRID_EXPORT_HARD_LIMIT_W={int(c.get('grid',{}).get('export_hard_limit_w',1000))}",
        f"GRID_DAY_EXPORT_W={int(c.get('grid',{}).get('day_export_w',1000))}",
        f"LOAD_BASE_HOUSE_W={float(c.get('load_model',{}).get('base_house_load_w',115))}",
        f"LOAD_SYSTEM_OVERHEAD_W={float(c.get('load_model',{}).get('system_overhead_w',130))}",
        f"LOAD_FORECAST_LEARNING_DAYS={int(lf.get('learning_days',14))}",
        f"LOAD_FORECAST_MIN_LEARNING_DAYS={int(lf.get('min_learning_days',3))}",
        f"LOAD_FORECAST_FALLBACK_HOUSE_W={float(lf.get('fallback_house_load_w',230))}",
        f"LOAD_FORECAST_MIN_HOUSE_W={float(lf.get('min_house_load_w',100))}",
        f"LOAD_FORECAST_MAX_HOUSE_W={float(lf.get('max_house_load_w',600))}",
        f"LOAD_FORECAST_SUBTRACT_SCHEDULED_LOADS={b(lf.get('subtract_scheduled_loads',lf.get('subtract_scheduled_heater',True)))}",
        "",
        f"PV_PERFORMANCE_RATIO={float(pv.get('performance_ratio',0.82))}",
        f"PV_WAKE_THRESHOLD_W={float(pv.get('wake_threshold_w',30))}",
        f"PV_USEFUL_THRESHOLD_W={float(pv.get('useful_threshold_w',250))}",
        f"PV_WAKEUP_BIAS_MINUTES={int(pv.get('wakeup_bias_minutes',15))}",
        "PV_ARRAYS_JSON=" + json.dumps(json.dumps(arrays, separators=(",", ":"), ensure_ascii=False)),
        "",
        f"NIGHT_MORNING_SURPLUS_THRESHOLD_W={float(ns.get('morning_surplus_threshold_w',350))}",
        f"NIGHT_SUSTAINED_MINUTES={int(ns.get('sustained_minutes',30))}",
        f"NIGHT_FLOOR_LEAD_MINUTES={int(ns.get('floor_lead_minutes',10))}",
        "",
        f"WATER_HEATER_ENABLED={b(wh.get('enabled',True))}",
        f"WATER_HEATER_TIME={j(wh_time)}",
        f"WATER_HEATER_POWER_W={float(wh.get('power_w',2000))}",
        f"WATER_HEATER_DURATION_MINUTES={int(wh.get('duration_minutes',90))}",
        f"WATER_HEATER_ENERGY_KWH={float(wh.get('scheduled_energy_kwh',3.0))}",
        f"COOKER_ENABLED={b(cooker.get('enabled',False))}",
        f"COOKER_TIME={j(cooker.get('scheduled_time','18:00'))}",
        f"COOKER_POWER_W={float(cooker.get('power_w',2000))}",
        f"COOKER_DURATION_MINUTES={int(cooker.get('duration_minutes',45))}",
        f"COOKER_ENERGY_KWH={'' if cooker.get('scheduled_energy_kwh') is None else float(cooker.get('scheduled_energy_kwh'))}",
        "",
        f"FORECAST_DEFAULT_SAFE_FACTOR={float(fu.get('default_safe_factor',0.80))}",
        f"FORECAST_LEARNING_DAYS={int(fu.get('learning_days',14))}",
        f"FORECAST_MIN_LEARNING_DAYS={int(fu.get('min_learning_days',5))}",
        f"FORECAST_LOWER_QUANTILE={float(fu.get('lower_quantile',0.20))}",
        f"FORECAST_MIN_FACTOR={float(fu.get('min_factor',0.60))}",
        f"FORECAST_MAX_FACTOR={float(fu.get('max_factor',1.05))}",
        "",
        f"DAY_AUTO_EXPORT_BUDGET_CONTROL={b(ds.get('auto_export_budget_control',True))}",
        f"DAY_CHARGE_EFFICIENCY={float(ds.get('charge_efficiency',0.91))}",
        f"DAY_EXPORT_SUPPORT_RESERVE_KWH={float(ds.get('export_support_reserve_kwh',1.5))}",
        f"DAY_MAX_TELEMETRY_AGE_MINUTES={float(ds.get('max_telemetry_age_minutes',15))}",
        f"DAY_MORNING_RESTORE_WINDOW_MINUTES={int(ds.get('morning_restore_window_minutes',90))}",
        f"DAY_MAX_BUDGET_WRITES_PER_DAY={int(ds.get('max_budget_writes_per_day',2))}",
        "",
        f"TELEMETRY_STALE_WARNING_MINUTES={float(th.get('stale_warning_minutes',10))}",
        f"TELEMETRY_CLOUD_OFFLINE_MINUTES={float(th.get('cloud_offline_minutes',20))}",
        "",
        f"CONTROL_LOOP_SECONDS={int(ctl.get('loop_seconds',60))}",
        f"CONTROL_FORECAST_REFRESH_MINUTES={int(ctl.get('forecast_refresh_minutes',60))}",
        f"CONTROL_SOC_FRESH_MINUTES={float(ctl.get('soc_fresh_minutes',5))}",
        f"CONTROL_SOC_ESTIMATE_MAX_MINUTES={float(ctl.get('soc_estimate_max_minutes',75))}",
        f"CONTROL_SOC_ESTIMATE_MAX_PV_W={float(ctl.get('soc_estimate_max_pv_w',100))}",
        f"CONTROL_SOC_ESTIMATE_MAX_DELTA_PCT={float(ctl.get('soc_estimate_max_delta_pct',15))}",
        f"CONTROL_MAX_TELEMETRY_AGE_MINUTES={float(ctl.get('max_control_telemetry_age_minutes',90))}",
        f"CONTROL_OFFLINE_RETRY_SECONDS={int(ctl.get('offline_retry_seconds',60))}",
        f"CONTROL_UNCERTAIN_SUBMIT_GUARD_MINUTES={int(ctl.get('uncertain_submit_guard_minutes',120))}",
        f"CONTROL_WRITE_API={j(ctl.get('write_api','power_update'))}",
        f"CONTROL_ORDER_STATUS_POLL_SECONDS={int(ctl.get('order_status_poll_seconds',15))}",
        f"CONTROL_FAILED_ORDER_RETRY_MINUTES={int(ctl.get('failed_order_retry_minutes',15))}",
        f"CONTROL_WRITE_STEP_W={int(ctl.get('write_step_w',100))}",
        f"CONTROL_MIN_WRITE_DELTA_W={int(ctl.get('min_write_delta_w',200))}",
        f"CONTROL_MIN_WRITE_INTERVAL_MINUTES={int(ctl.get('min_write_interval_minutes',120))}",
        f"CONTROL_MAX_SUCCESSFUL_WRITES_PER_DAY={int(ctl.get('max_successful_writes_per_day',ctl.get('max_writes_per_day',4)))}",
        "CONTROL_BONUS_WRITE_ENABLED=true",
        "CONTROL_BONUS_WRITE_DELTA_W=500",
        "CONTROL_MAX_SUCCESSFUL_WRITES_WITH_BONUS=5",
        "CONTROL_MAX_ORDER_SUBMISSIONS_PER_DAY=8",
        f"CONTROL_MAX_NIGHT_CORRECTIONS={int(ctl.get('max_night_corrections',1))}",
        f"CONTROL_SOC_CORRECTION_THRESHOLD_PCT={float(ctl.get('soc_correction_threshold_pct',5.0))}",
        f"CONTROL_NIGHT_START_MINUTES_BEFORE_SUNSET={int(ctl.get('night_start_minutes_before_sunset',30))}",
        f"CONTROL_DRY_RUN={b(ctl.get('dry_run',True))}",
        "",
        f"LOGGING_STATE_DB={j(c.get('logging',{}).get('state_db','/var/lib/deye-solar-optimizer/state.db'))}",
        f"LOGGING_JSONL={j(c.get('logging',{}).get('jsonl_log','/var/lib/deye-solar-optimizer/events.jsonl'))}",
        f"LOGGING_BUNDLE_DIR={j(bundle_dir)}",
    ]

    out = Path(args.output)
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
