#!/usr/bin/env python3
"""Environment-file configuration for Deye Solar Optimizer v3.1.0.

v3 uses a single .env file as the runtime source of truth.  The parser is deliberately
small and dependency-free so the package only needs Python's standard library.
Real process environment variables override values loaded from the .env file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable

DEFAULT_ENV = "/etc/deye-solar-optimizer/deye.env"


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return str(json.loads(value))
        except Exception:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def read_env_file(path: str | os.PathLike[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{p}:{lineno}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            raise ValueError(f"{p}:{lineno}: invalid environment key {key!r}")
        # Inline comments are intentionally not stripped: JSON and passwords may contain '#'.
        result[key] = _strip_quotes(value)
    return result


def _env(path: str | os.PathLike[str]) -> Dict[str, str]:
    values = read_env_file(path)
    # Process environment wins, making systemd/container overrides straightforward.
    for key in list(values):
        if key in os.environ:
            values[key] = os.environ[key]
    # Also accept keys that exist only in the process environment.
    for key, value in os.environ.items():
        if key.startswith(("DEYE_", "SITE_", "STRATEGY_", "BATTERY_", "GRID_", "LOAD_", "PV_", "NIGHT_", "WATER_", "COOKER_", "FORECAST_", "DAY_", "TELEMETRY_", "CONTROL_", "LOGGING_", "ANALYTICS_", "ECONOMIC_", "MORNING_")):
            values.setdefault(key, value)
    return values


def _get(v: Dict[str, str], key: str, default: Any = None) -> Any:
    value = v.get(key)
    if value is None or value == "":
        return default
    return value


def _bool(v: Dict[str, str], key: str, default: bool = False) -> bool:
    value = _get(v, key, None)
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    raise ValueError(f"{key} must be true/false, got {value!r}")


def _int(v: Dict[str, str], key: str, default: int) -> int:
    return int(float(_get(v, key, default)))


def _float(v: Dict[str, str], key: str, default: float) -> float:
    return float(_get(v, key, default))


def _optional_float(v: Dict[str, str], key: str) -> float | None:
    value = _get(v, key, None)
    return None if value is None else float(value)


def _float_list(v: Dict[str, str], key: str, default: str) -> list[float]:
    text = str(_get(v, key, default))
    out: list[float] = []
    for item in text.split(","):
        item = item.strip()
        if item:
            out.append(float(item))
    return out


def _int_list(v: Dict[str, str], key: str, default: str) -> list[int]:
    return [int(round(x)) for x in _float_list(v, key, default)]


def _scheduled_load(v: Dict[str, str], prefix: str, *, default_time: str, default_power: float, default_duration: int, default_enabled: bool) -> Dict[str, Any]:
    power_w = max(0.0, _float(v, f"{prefix}_POWER_W", default_power))
    duration = max(0, _int(v, f"{prefix}_DURATION_MINUTES", default_duration))
    energy = _optional_float(v, f"{prefix}_ENERGY_KWH")
    if energy is None:
        energy = power_w / 1000.0 * duration / 60.0
    return {
        "enabled": _bool(v, f"{prefix}_ENABLED", default_enabled),
        "scheduled_time": str(_get(v, f"{prefix}_TIME", default_time)),
        "power_w": power_w,
        "duration_minutes": duration,
        "scheduled_energy_kwh": max(0.0, float(energy)),
    }


def load_config(path: str = DEFAULT_ENV) -> Dict[str, Any]:
    v = _env(path)

    arrays_text = str(_get(v, "PV_ARRAYS_JSON", "[]"))
    try:
        arrays = json.loads(arrays_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"PV_ARRAYS_JSON is invalid JSON: {exc}") from exc
    if not isinstance(arrays, list) or not arrays:
        raise ValueError("PV_ARRAYS_JSON must be a non-empty JSON array")
    clean_arrays = []
    for idx, item in enumerate(arrays):
        if not isinstance(item, dict):
            raise ValueError(f"PV_ARRAYS_JSON item {idx} must be an object")
        clean_arrays.append({
            "name": str(item.get("name", f"array{idx+1}")),
            "kwp": float(item["kwp"]),
            "tilt_deg": float(item["tilt_deg"]),
            "azimuth_deg": float(item["azimuth_deg"]),
        })

    cfg: Dict[str, Any] = {
        "site": {
            "latitude": _float(v, "SITE_LATITUDE", 0.0),
            "longitude": _float(v, "SITE_LONGITUDE", 0.0),
            "timezone": str(_get(v, "SITE_TIMEZONE", "Etc/UTC")),
        },
        "deye": {
            "station_id": _int(v, "DEYE_STATION_ID", 0),
            "inverter_sn": str(_get(v, "DEYE_INVERTER_SN", "")),
            "base_url": str(_get(v, "DEYE_BASE_URL", "https://eu1-developer.deyecloud.com/v1.0")),
            "app_id": str(_get(v, "DEYE_APP_ID", "")),
            "app_secret": str(_get(v, "DEYE_APP_SECRET", "")),
            "login": str(_get(v, "DEYE_LOGIN", "")),
            "password": str(_get(v, "DEYE_PASSWORD", "")),
        },
        "strategy": {"active": str(_get(v, "STRATEGY_ACTIVE", "conservative"))},
        "battery": {
            "effective_kwh": _float(v, "BATTERY_EFFECTIVE_KWH", 15.0),
            "soc_floor_pct": _float(v, "BATTERY_SOC_FLOOR_PCT", 15.0),
            "day_target_soc_pct": _float(v, "BATTERY_DAY_TARGET_SOC_PCT", 96.0),
        },
        "maintenance": {
            "target_soc_pct": _float(v, "BATTERY_MAINTENANCE_TARGET_SOC_PCT", 100.0),
            "interval_days": _int(v, "BATTERY_MAINTENANCE_INTERVAL_DAYS", 30),
            "full_soc_threshold_pct": _float(v, "BATTERY_FULL_SOC_THRESHOLD_PCT", 99.5),
        },
        "grid": {
            "export_hard_limit_w": _int(v, "GRID_EXPORT_HARD_LIMIT_W", 1000),
            "day_export_w": _int(v, "GRID_DAY_EXPORT_W", 1000),
        },
        "load_model": {
            "base_house_load_w": _float(v, "LOAD_BASE_HOUSE_W", 115.0),
            "system_overhead_w": _float(v, "LOAD_SYSTEM_OVERHEAD_W", 130.0),
        },
        "load_forecast": {
            "learning_days": _int(v, "LOAD_FORECAST_LEARNING_DAYS", 14),
            "min_learning_days": _int(v, "LOAD_FORECAST_MIN_LEARNING_DAYS", 3),
            "fallback_house_load_w": _float(v, "LOAD_FORECAST_FALLBACK_HOUSE_W", 230.0),
            "min_house_load_w": _float(v, "LOAD_FORECAST_MIN_HOUSE_W", 100.0),
            "max_house_load_w": _float(v, "LOAD_FORECAST_MAX_HOUSE_W", 600.0),
            "subtract_scheduled_loads": _bool(v, "LOAD_FORECAST_SUBTRACT_SCHEDULED_LOADS", True),
            # legacy key retained for code that still checks it
            "subtract_scheduled_heater": _bool(v, "LOAD_FORECAST_SUBTRACT_SCHEDULED_LOADS", True),
        },
        "pv": {
            "performance_ratio": _float(v, "PV_PERFORMANCE_RATIO", 0.82),
            "wake_threshold_w": _float(v, "PV_WAKE_THRESHOLD_W", 30.0),
            "useful_threshold_w": _float(v, "PV_USEFUL_THRESHOLD_W", 250.0),
            "wakeup_bias_minutes": _int(v, "PV_WAKEUP_BIAS_MINUTES", 15),
            "arrays": clean_arrays,
        },
        "night_strategy": {
            "morning_surplus_threshold_w": _float(v, "NIGHT_MORNING_SURPLUS_THRESHOLD_W", 350.0),
            "sustained_minutes": _int(v, "NIGHT_SUSTAINED_MINUTES", 30),
            "floor_lead_minutes": _int(v, "NIGHT_FLOOR_LEAD_MINUTES", 10),
        },
        "water_heater": _scheduled_load(v, "WATER_HEATER", default_time="10:00", default_power=2000.0, default_duration=90, default_enabled=False),
        "cooker": _scheduled_load(v, "COOKER", default_time="18:00", default_power=2000.0, default_duration=45, default_enabled=False),
        "forecast_uncertainty": {
            "default_safe_factor": _float(v, "FORECAST_DEFAULT_SAFE_FACTOR", 0.80),
            "learning_days": _int(v, "FORECAST_LEARNING_DAYS", 14),
            "min_learning_days": _int(v, "FORECAST_MIN_LEARNING_DAYS", 5),
            "lower_quantile": _float(v, "FORECAST_LOWER_QUANTILE", 0.20),
            "min_factor": _float(v, "FORECAST_MIN_FACTOR", 0.60),
            "max_factor": _float(v, "FORECAST_MAX_FACTOR", 1.05),
            "probabilistic_enabled": _bool(v, "FORECAST_PROBABILISTIC_ENABLED", True),
            "probabilistic_min_days": _int(v, "FORECAST_PROBABILISTIC_MIN_DAYS", 10),
            "probabilistic_learning_days": _int(v, "FORECAST_PROBABILISTIC_LEARNING_DAYS", 45),
            "probabilistic_quantiles": _float_list(v, "FORECAST_PROBABILISTIC_QUANTILES", "0.10,0.20,0.50,0.80,0.90"),
            "weather_ratio_enabled": _bool(v, "FORECAST_WEATHER_RATIO_ENABLED", True),
            "observed_irradiance_days": _int(v, "FORECAST_OBSERVED_IRRADIANCE_DAYS", 7),
        },
        "morning_learning": {
            "enabled": _bool(v, "MORNING_LEARNING_ENABLED", True),
            "min_days": _int(v, "MORNING_LEARNING_MIN_DAYS", 7),
            "learning_days": _int(v, "MORNING_LEARNING_DAYS", 30),
            "max_abs_minutes": _int(v, "MORNING_LEARNING_MAX_ABS_MINUTES", 45),
        },
        "day_strategy": {
            "auto_export_budget_control": _bool(v, "DAY_AUTO_EXPORT_BUDGET_CONTROL", True),
            "charge_efficiency": _float(v, "DAY_CHARGE_EFFICIENCY", 0.91),
            "export_support_reserve_kwh": _float(v, "DAY_EXPORT_SUPPORT_RESERVE_KWH", 1.5),
            "max_telemetry_age_minutes": _float(v, "DAY_MAX_TELEMETRY_AGE_MINUTES", 15.0),
            "morning_restore_window_minutes": _int(v, "DAY_MORNING_RESTORE_WINDOW_MINUTES", 90),
            "max_budget_writes_per_day": _int(v, "DAY_MAX_BUDGET_WRITES_PER_DAY", 2),
            "curtailment_override_enabled": _bool(v, "DAY_CURTAILMENT_OVERRIDE_ENABLED", True),
            "curtailment_override_soc_pct": _float(v, "DAY_CURTAILMENT_OVERRIDE_SOC_PCT", 98.0),
            "curtailment_override_charge_w": _float(v, "DAY_CURTAILMENT_OVERRIDE_CHARGE_W", 300.0),
            "curtailment_override_min_pv_w": _float(v, "DAY_CURTAILMENT_OVERRIDE_MIN_PV_W", 100.0),
            "intraday_clip_soc_hysteresis_pct": _float(v, "DAY_INTRADAY_CLIP_SOC_HYSTERESIS_PCT", 5.0),
            "intraday_bias_enabled": _bool(v, "DAY_INTRADAY_BIAS_ENABLED", True),
            "intraday_bias_window_hours": _float(v, "DAY_INTRADAY_BIAS_WINDOW_HOURS", 3.0),
            "intraday_bias_min_forecast_kwh": _float(v, "DAY_INTRADAY_BIAS_MIN_FORECAST_KWH", 1.0),
            "intraday_bias_trust_kwh": _float(v, "DAY_INTRADAY_BIAS_TRUST_KWH", 3.0),
            "intraday_bias_min": _float(v, "DAY_INTRADAY_BIAS_MIN", 0.40),
            "intraday_bias_max": _float(v, "DAY_INTRADAY_BIAS_MAX", 1.60),
            "load_scaled_reserve_enabled": _bool(v, "DAY_LOAD_SCALED_RESERVE_ENABLED", True),
            "reserve_min_kwh": _float(v, "DAY_RESERVE_MIN_KWH", 0.5),
            "reserve_max_kwh": _float(v, "DAY_RESERVE_MAX_KWH", 6.0),
            "above_cap_refill_enabled": _bool(v, "DAY_ABOVE_CAP_REFILL_ENABLED", True),
            "staleness_floor_enabled": _bool(v, "DAY_STALENESS_FLOOR_ENABLED", True),
            "staleness_floor_w": _int(v, "DAY_STALENESS_FLOOR_W", 300),
        },
        "telemetry_health": {
            "stale_warning_minutes": _float(v, "TELEMETRY_STALE_WARNING_MINUTES", 10.0),
            "cloud_offline_minutes": _float(v, "TELEMETRY_CLOUD_OFFLINE_MINUTES", 20.0),
        },
        "control": {
            "loop_seconds": _int(v, "CONTROL_LOOP_SECONDS", 60),
            "forecast_refresh_minutes": _int(v, "CONTROL_FORECAST_REFRESH_MINUTES", 60),
            "soc_fresh_minutes": _float(v, "CONTROL_SOC_FRESH_MINUTES", 5.0),
            "soc_estimate_max_minutes": _float(v, "CONTROL_SOC_ESTIMATE_MAX_MINUTES", 75.0),
            "soc_estimate_max_pv_w": _float(v, "CONTROL_SOC_ESTIMATE_MAX_PV_W", 100.0),
            "soc_estimate_max_delta_pct": _float(v, "CONTROL_SOC_ESTIMATE_MAX_DELTA_PCT", 15.0),
            "max_control_telemetry_age_minutes": _float(v, "CONTROL_MAX_TELEMETRY_AGE_MINUTES", 90.0),
            "offline_retry_seconds": _int(v, "CONTROL_OFFLINE_RETRY_SECONDS", 60),
            "uncertain_submit_guard_minutes": _int(v, "CONTROL_UNCERTAIN_SUBMIT_GUARD_MINUTES", 120),
            "write_api": str(_get(v, "CONTROL_WRITE_API", "power_update")),
            "order_status_poll_seconds": _int(v, "CONTROL_ORDER_STATUS_POLL_SECONDS", 15),
            "failed_order_retry_minutes": _int(v, "CONTROL_FAILED_ORDER_RETRY_MINUTES", 15),
            "device_reject_retry_minutes": _int(v, "CONTROL_DEVICE_REJECT_RETRY_MINUTES", 45),
            "device_reject_error_codes": str(_get(v, "CONTROL_DEVICE_REJECT_ERROR_CODES", "540")),
            "write_step_w": _int(v, "CONTROL_WRITE_STEP_W", 100),
            "min_write_delta_w": _int(v, "CONTROL_MIN_WRITE_DELTA_W", 200),
            "min_write_interval_minutes": _int(v, "CONTROL_MIN_WRITE_INTERVAL_MINUTES", 120),
            # v3: confirmed successful writes consume the wear budget; failed status=500 orders do not.
            "max_successful_writes_per_day": _int(v, "CONTROL_MAX_SUCCESSFUL_WRITES_PER_DAY", 4),
            "max_writes_per_day": _int(v, "CONTROL_MAX_SUCCESSFUL_WRITES_PER_DAY", 4),
            "bonus_write_enabled": _bool(v, "CONTROL_BONUS_WRITE_ENABLED", True),
            "bonus_write_delta_w": _int(v, "CONTROL_BONUS_WRITE_DELTA_W", 500),
            "max_successful_writes_with_bonus": _int(v, "CONTROL_MAX_SUCCESSFUL_WRITES_WITH_BONUS", 5),
            "max_order_submissions_per_day": _int(v, "CONTROL_MAX_ORDER_SUBMISSIONS_PER_DAY", 8),
            "max_night_corrections": _int(v, "CONTROL_MAX_NIGHT_CORRECTIONS", 1),
            "soc_correction_threshold_pct": _float(v, "CONTROL_SOC_CORRECTION_THRESHOLD_PCT", 5.0),
            "night_start_minutes_before_sunset": _int(v, "CONTROL_NIGHT_START_MINUTES_BEFORE_SUNSET", 30),
            "dry_run": _bool(v, "CONTROL_DRY_RUN", True),
        },
        "analytics": {
            "enabled": _bool(v, "ANALYTICS_ENABLED", True),
            "bind": str(_get(v, "ANALYTICS_BIND", "127.0.0.1")),
            "port": _int(v, "ANALYTICS_PORT", 8787),
            "history_days": _int(v, "ANALYTICS_HISTORY_DAYS", 30),
            "replay_caps_w": _int_list(v, "ANALYTICS_REPLAY_CAPS_W", "0,300,500,800,1000"),
            "battery_charge_efficiency": _float(v, "ANALYTICS_BATTERY_CHARGE_EFFICIENCY", _float(v, "DAY_CHARGE_EFFICIENCY", 0.91)),
            "battery_discharge_efficiency": _float(v, "ANALYTICS_BATTERY_DISCHARGE_EFFICIENCY", 0.95),
            "battery_max_charge_w": _float(v, "ANALYTICS_BATTERY_MAX_CHARGE_W", 10000.0),
            "battery_max_discharge_w": _float(v, "ANALYTICS_BATTERY_MAX_DISCHARGE_W", 10000.0),
            "battery_soc_ceiling_pct": _float(v, "ANALYTICS_BATTERY_SOC_CEILING_PCT", 100.0),
            "oracle_soc_step_pct": _float(v, "ANALYTICS_ORACLE_SOC_STEP_PCT", 0.5),
            "oracle_export_step_w": _int(v, "ANALYTICS_ORACLE_EXPORT_STEP_W", 100),
            "curtailment_soc_threshold_pct": _float(v, "ANALYTICS_CURTAILMENT_SOC_THRESHOLD_PCT", 98.0),
            "curtailment_export_margin_w": _float(v, "ANALYTICS_CURTAILMENT_EXPORT_MARGIN_W", 100.0),
            "curtailment_min_gap_w": _float(v, "ANALYTICS_CURTAILMENT_MIN_GAP_W", 300.0),
            "curtailment_max_charge_w": _float(v, "ANALYTICS_CURTAILMENT_MAX_CHARGE_W", 300.0),
            "curtailment_array_ceiling_w": _float(v, "ANALYTICS_CURTAILMENT_ARRAY_CEILING_W", 0.0),
            "curtailment_max_kwh_per_kwp": _float(v, "ANALYTICS_CURTAILMENT_MAX_KWH_PER_KWP", 3.6),
            "mppt_map": json.loads(str(_get(v, "ANALYTICS_MPPT_MAP_JSON", "{}"))),
        },
        "economic": {
            "import_eur_kwh": _float(v, "ECONOMIC_IMPORT_EUR_KWH", 0.25),
            "export_eur_kwh": _float(v, "ECONOMIC_EXPORT_EUR_KWH", 0.00),
            "battery_wear_eur_kwh": _float(v, "ECONOMIC_BATTERY_WEAR_EUR_KWH", 0.00),
        },
        "logging": {
            "state_db": str(_get(v, "LOGGING_STATE_DB", "/var/lib/deye-solar-optimizer/state.db")),
            "jsonl_log": str(_get(v, "LOGGING_JSONL", "/var/lib/deye-solar-optimizer/events.jsonl")),
            "bundle_dir": str(_get(v, "LOGGING_BUNDLE_DIR", "/var/lib/deye-solar-optimizer/exports")),
        },
    }
    validate_config(cfg)
    return cfg


def validate_config(raw: Dict[str, Any]) -> None:
    from strategy_presets import active_strategy

    limit = int(raw["grid"]["export_hard_limit_w"])
    day = int(raw["grid"]["day_export_w"])
    if limit <= 0 or limit > 100000:
        raise ValueError("GRID_EXPORT_HARD_LIMIT_W must be positive and <=100000 W")
    if not (0 <= day <= limit):
        raise ValueError("GRID_DAY_EXPORT_W must be between 0 and the hard limit")
    soc = float(raw["battery"]["soc_floor_pct"])
    if not 5 <= soc <= 40:
        raise ValueError("BATTERY_SOC_FLOOR_PCT outside conservative range 5..40")
    if float(raw["battery"]["effective_kwh"]) <= 0:
        raise ValueError("BATTERY_EFFECTIVE_KWH must be positive")
    if str(raw["control"].get("write_api", "power_update")) not in {"power_update", "dynamic_control"}:
        raise ValueError("CONTROL_WRITE_API must be power_update or dynamic_control")
    port = int(raw.get("analytics", {}).get("port", 8787))
    if not (1 <= port <= 65535):
        raise ValueError("ANALYTICS_PORT must be 1..65535")
    mppt_map = raw.get("analytics", {}).get("mppt_map", {})
    if not isinstance(mppt_map, dict):
        raise ValueError("ANALYTICS_MPPT_MAP_JSON must decode to an object")
    qs = raw.get("forecast_uncertainty", {}).get("probabilistic_quantiles", [])
    if not qs or any(float(q) < 0 or float(q) > 1 for q in qs):
        raise ValueError("FORECAST_PROBABILISTIC_QUANTILES must contain values in 0..1")
    if not raw["deye"].get("inverter_sn"):
        raise ValueError("DEYE_INVERTER_SN is required")
    for key in ("app_id", "app_secret", "login", "password"):
        if not raw["deye"].get(key):
            raise ValueError(f"DEYE_{key.upper()} is required")
    active_strategy(raw)


def make_deye_client(raw: Dict[str, Any], timeout: int = 12):
    from deye_api import DeyeClient
    d = raw["deye"]
    return DeyeClient(
        d["base_url"],
        None,
        timeout=timeout,
        app_id=d["app_id"],
        app_secret=d["app_secret"],
        login=d["login"],
        password=d["password"],
    )


def redact_env_lines(path: str | os.PathLike[str]) -> str:
    secret_keys = {"DEYE_APP_ID", "DEYE_APP_SECRET", "DEYE_LOGIN", "DEYE_PASSWORD"}
    out: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(raw)
            continue
        key = stripped.split("=", 1)[0].replace("export ", "").strip()
        if key in secret_keys:
            prefix = raw[: raw.index("=") + 1]
            out.append(prefix + '"***REDACTED***"')
        else:
            out.append(raw)
    return "\n".join(out).rstrip() + "\n"
