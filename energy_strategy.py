#!/usr/bin/env python3
"""Energy-budget helpers for Deye Solar Optimizer v3.1.0.

No Deye write calls live here.  Forecasts, SOC, recent history and a named strategy
are converted into low-churn MAX_SELL_POWER recommendations for controller.py.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from strategy_presets import active_strategy, effective_safe_factor


@dataclass
class BatteryTarget:
    target_soc_pct: float
    maintenance_due: bool
    last_full_at: Optional[dt.datetime]
    reason: str


@dataclass
class MorningSocPlan:
    strategy_tag: str
    deadline: dt.datetime
    desired_soc_pct: float
    projected_soc_pct: float
    export_w: int
    reason: str


@dataclass
class DayEnergyPlan:
    strategy_tag: str
    planning_start_at: str
    planning_soc_pct: float
    morning_desired_soc_pct: Optional[float]
    morning_projected_soc_pct: Optional[float]
    target_soc_pct: float
    target_reason: str
    maintenance_due: bool
    last_full_at: Optional[str]
    raw_remaining_pv_kwh: float
    base_safe_forecast_factor: float
    safe_forecast_factor: float
    intraday_bias: float
    intraday_bias_source: str
    bias_corrected_pv_kwh: float
    safe_remaining_pv_kwh: float
    forecast_factor_source: str
    forecast_distribution: Dict[str, float]
    forecast_distribution_source: str
    house_load_w: float
    house_load_source: str
    house_energy_kwh: float
    system_overhead_kwh: float
    water_heater_kwh: float
    water_heater_status: str
    cooker_kwh: float
    cooker_status: str
    scheduled_loads_kwh: float
    battery_stored_kwh_needed: float
    battery_input_kwh_needed: float
    reserve_kwh: float
    reserve_source: str
    end_of_day_target_soc_pct: float
    end_of_day_target_reason: str
    night_energy_need_kwh: float
    night_hours: float
    pv_surplus_kwh: float
    stored_surplus_kwh: float
    surplus_above_cap_kwh: float
    deficit_covered_by_above_cap_kwh: float
    battery_headroom_kwh: float
    forced_export_kwh: float
    export_energy_budget_kwh: float
    hours_to_sunset: float
    cap_sustain_hours: float
    staleness_floor_w: int
    staleness_floor_reason: str
    full_export_margin_kwh: float
    recommended_export_w: int
    allocation_mode: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _parse_hhmm(text: str) -> Tuple[int, int]:
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid HH:MM time: {text}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HH:MM time: {text}")
    return hour, minute


def quantize_export_floor(watts: float, step_w: int, hard_limit_w: int) -> int:
    """Quantize down so an energy budget is never rounded upward."""
    step = max(1, int(step_w or 100))
    value = max(0.0, min(float(hard_limit_w), float(watts)))
    return int(math.floor(value / step) * step)


def quantize_export_nearest(watts: float, step_w: int, hard_limit_w: int) -> int:
    step = max(1, int(step_w or 100))
    value = max(0.0, min(float(hard_limit_w), float(watts)))
    return int(max(0, min(hard_limit_w, round(value / step) * step)))


def economic_battery_export_profitable(raw: Dict[str, Any]) -> bool:
    """Whether deliberate battery-to-grid cycling has positive configured unit economics.

    This is a static-price approximation. Genuine PV surplus may still be exported when
    this is false. Dynamic/time-of-use pricing is intentionally outside v3.1.
    """
    econ = raw.get("economic", {})
    export_price = float(econ.get("export_eur_kwh", 0.0))
    import_price = float(econ.get("import_eur_kwh", 0.0))
    wear = float(econ.get("battery_wear_eur_kwh", 0.0))
    discharge_eff = max(0.5, min(1.0, float(raw.get("analytics", {}).get("battery_discharge_efficiency", 0.95))))
    opportunity = import_price / discharge_eff + wear
    return export_price > opportunity + 1e-9


def sustained_pv_time(
    day: Any,
    *,
    threshold_w: float,
    sustained_minutes: int,
    site_bias_minutes: int = 0,
) -> dt.datetime:
    """First forecast point where PV stays above threshold for the requested period."""
    points = list(getattr(day, "points", []) or [])
    required = max(1, int(math.ceil(max(1, sustained_minutes) / 15.0)))
    for i, point in enumerate(points):
        if point.time < day.sunrise:
            continue
        window = points[i : i + required]
        if len(window) < required:
            break
        if all(float(p.predicted_w) >= float(threshold_w) for p in window):
            return point.time + dt.timedelta(minutes=int(site_bias_minutes))
    fallback = (
        getattr(day, "useful_pv_start", None)
        or getattr(day, "pv_wakeup", None)
        or day.sunrise
    )
    return fallback


def night_floor_deadline(raw: Dict[str, Any], day: Any) -> dt.datetime:
    ns = raw.get("night_strategy", {})
    threshold = float(ns.get("morning_surplus_threshold_w", 350.0))
    sustained = int(ns.get("sustained_minutes", 30))
    lead = int(ns.get("floor_lead_minutes", 10))
    site_bias = int(raw.get("pv", {}).get("wakeup_bias_minutes", 0))
    usable = sustained_pv_time(
        day,
        threshold_w=threshold,
        sustained_minutes=sustained,
        site_bias_minutes=site_bias,
    )
    deadline = usable - dt.timedelta(minutes=lead)
    return max(day.sunrise, deadline)


def scheduled_load_daily_energy_kwh(raw: Dict[str, Any], section: str) -> float:
    load = raw.get(section, {})
    if not bool(load.get("enabled", False)):
        return 0.0
    if load.get("scheduled_energy_kwh") is not None:
        return max(0.0, float(load.get("scheduled_energy_kwh", 0.0)))
    power_w = max(0.0, float(load.get("power_w", 0.0)))
    duration_min = max(0.0, float(load.get("duration_minutes", 0.0)))
    return power_w / 1000.0 * duration_min / 60.0


def scheduled_load_remaining_kwh(raw: Dict[str, Any], section: str, now: dt.datetime, default_time: str) -> Tuple[float, str]:
    load = raw.get(section, {})
    if not bool(load.get("enabled", False)):
        return 0.0, "disabled"
    energy = scheduled_load_daily_energy_kwh(raw, section)
    duration = max(1.0, float(load.get("duration_minutes", 60)))
    hour, minute = _parse_hhmm(str(load.get("scheduled_time", default_time)))
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end = start + dt.timedelta(minutes=duration)
    if now < start:
        return energy, f"scheduled_{start.strftime('%H:%M')}"
    if now >= end:
        return 0.0, "completed_or_past"
    fraction = (end - now).total_seconds() / max(1.0, (end - start).total_seconds())
    return energy * max(0.0, min(1.0, fraction)), "running_window"


def heater_daily_energy_kwh(raw: Dict[str, Any]) -> float:
    return scheduled_load_daily_energy_kwh(raw, "water_heater")


def heater_remaining_kwh(raw: Dict[str, Any], now: dt.datetime) -> Tuple[float, str]:
    return scheduled_load_remaining_kwh(raw, "water_heater", now, "10:00")


def cooker_daily_energy_kwh(raw: Dict[str, Any]) -> float:
    return scheduled_load_daily_energy_kwh(raw, "cooker")


def cooker_remaining_kwh(raw: Dict[str, Any], now: dt.datetime) -> Tuple[float, str]:
    return scheduled_load_remaining_kwh(raw, "cooker", now, "18:00")


def scheduled_loads_daily_energy_kwh(raw: Dict[str, Any]) -> float:
    return heater_daily_energy_kwh(raw) + cooker_daily_energy_kwh(raw)


def battery_target(raw: Dict[str, Any], db: Any, now: dt.datetime) -> BatteryTarget:
    normal = float(raw.get("battery", {}).get("day_target_soc_pct", 96.0))
    maint = raw.get("maintenance", {})
    maintenance_target = float(maint.get("target_soc_pct", 100.0))
    interval_days = max(1, int(maint.get("interval_days", 30)))
    threshold = float(maint.get("full_soc_threshold_pct", 99.5))

    last_full = None
    stored = db.get("last_full_balance_at") if db is not None else None
    if stored:
        try:
            last_full = dt.datetime.fromisoformat(str(stored)).astimezone(now.tzinfo)
        except Exception:
            last_full = None
    if last_full is None and db is not None and hasattr(db, "last_full_soc_at"):
        try:
            last_full = db.last_full_soc_at(threshold)
            if last_full is not None:
                last_full = last_full.astimezone(now.tzinfo)
        except Exception:
            last_full = None

    due = last_full is None or (now - last_full) >= dt.timedelta(days=interval_days)
    if due:
        return BatteryTarget(maintenance_target, True, last_full, f"maintenance_due_{interval_days}d")
    return BatteryTarget(normal, False, last_full, "normal_daily_target")


def learned_house_load_w(raw: Dict[str, Any], db: Any, now: dt.datetime) -> Tuple[float, str]:
    lf = raw.get("load_forecast", {})
    fallback = float(lf.get("fallback_house_load_w", 230.0))
    learning_days = max(1, int(lf.get("learning_days", 14)))
    min_days = max(1, int(lf.get("min_learning_days", 3)))
    lo = float(lf.get("min_house_load_w", 100.0))
    hi = float(lf.get("max_house_load_w", 600.0))
    if db is None or not hasattr(db, "daily_energy_history"):
        return max(lo, min(hi, fallback)), "fallback"
    try:
        hist = db.daily_energy_history(now.date(), learning_days)
    except Exception:
        hist = []
    scheduled_kwh = scheduled_loads_daily_energy_kwh(raw) if bool(lf.get("subtract_scheduled_loads", lf.get("subtract_scheduled_heater", True))) else 0.0
    values = []
    for row in hist:
        total = row.get("consumption_kwh")
        if total is None:
            continue
        ordinary = max(0.0, float(total) - scheduled_kwh)
        values.append(ordinary / 24.0 * 1000.0)
    if len(values) >= min_days:
        learned = statistics.median(values)
        return max(lo, min(hi, learned)), f"median_{len(values)}d"
    return max(lo, min(hi, fallback)), f"fallback_history_{len(values)}d"


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError("empty values")
    vals = sorted(float(v) for v in values)
    if len(vals) == 1:
        return vals[0]
    q = max(0.0, min(1.0, float(q)))
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def forecast_distribution(
    raw: Dict[str, Any], db: Any, now: dt.datetime, target_date: Optional[dt.date] = None
) -> Tuple[Dict[str, float], str]:
    """Learn P10/P20/P50/P80/P90 forecast multipliers after enough comparable days.

    Until the configured minimum history is available this returns an empty distribution,
    so control remains on the explicit fallback factor. Forecasts are grouped by lead-time
    bucket (day-ahead, early morning, etc.) because their errors are materially different.
    """
    fu = raw.get("forecast_uncertainty", {})
    if not bool(fu.get("probabilistic_enabled", True)) or db is None:
        return {}, "probabilistic_disabled"
    min_samples = max(1, int(fu.get("probabilistic_min_days", 10)))
    learning_days = max(min_samples, int(fu.get("probabilistic_learning_days", 45)))
    quantiles = list(fu.get("probabilistic_quantiles", [0.10, 0.20, 0.50, 0.80, 0.90]))
    target_date = target_date or now.date()
    try:
        from state_db import forecast_lead_bucket
        bucket = forecast_lead_bucket(now, target_date)
        ratios = db.forecast_accuracy_ratios_for_bucket(now.date(), learning_days, bucket)
    except Exception:
        ratios = []
        bucket = "unknown"
    if len(ratios) < min_samples:
        return {}, f"probabilistic_wait_{bucket}_{len(ratios)}/{min_samples}"
    lo = float(fu.get("min_factor", 0.60))
    hi = float(fu.get("max_factor", 1.05))
    dist: Dict[str, float] = {}
    for q in quantiles:
        qf = max(0.0, min(1.0, float(q)))
        label = f"p{int(round(qf*100)):02d}"
        dist[label] = max(lo, min(hi, _quantile(ratios, qf)))
    return dist, f"probabilistic_{bucket}_{len(ratios)}d"


def safe_forecast_factor(
    raw: Dict[str, Any], db: Any, now: dt.datetime, target_date: Optional[dt.date] = None
) -> Tuple[float, str]:
    fu = raw.get("forecast_uncertainty", {})
    default = float(fu.get("default_safe_factor", 0.80))
    quantile = float(fu.get("lower_quantile", 0.20))
    lo = float(fu.get("min_factor", 0.60))
    hi = float(fu.get("max_factor", 1.05))

    dist, source = forecast_distribution(raw, db, now, target_date)
    label = f"p{int(round(max(0.0,min(1.0,quantile))*100)):02d}"
    if dist:
        if label in dist:
            return dist[label], f"{source}_{label}"
        # Quantile not explicitly configured: interpolate directly from same bucket ratios.
        try:
            from state_db import forecast_lead_bucket
            td = target_date or now.date()
            bucket = forecast_lead_bucket(now, td)
            ratios = db.forecast_accuracy_ratios_for_bucket(now.date(), int(fu.get("probabilistic_learning_days",45)), bucket)
            return max(lo, min(hi, _quantile(ratios, quantile))), f"{source}_q{int(quantile*100)}"
        except Exception:
            pass

    # Pre-probabilistic fallback keeps v3 behavior.
    learning_days = max(1, int(fu.get("learning_days", 14)))
    min_samples = max(1, int(fu.get("min_learning_days", 5)))
    if db is None or not hasattr(db, "forecast_accuracy_ratios"):
        return max(lo, min(hi, default)), "default"
    try:
        ratios = db.forecast_accuracy_ratios(now.date(), learning_days)
    except Exception:
        ratios = []
    if len(ratios) < min_samples:
        return max(lo, min(hi, default)), f"default_history_{len(ratios)}d+{source}"
    learned = _quantile(ratios, quantile)
    return max(lo, min(hi, learned)), f"legacy_q{int(quantile*100)}_{len(ratios)}d+{source}"


def learned_morning_bias_minutes(raw: Dict[str, Any], db: Any, now: dt.datetime) -> Tuple[float, str]:
    ml = raw.get("morning_learning", {})
    if not bool(ml.get("enabled", True)) or db is None or not hasattr(db, "morning_timing_errors"):
        return 0.0, "disabled_or_unavailable"
    min_days = max(1, int(ml.get("min_days", 7)))
    days = max(min_days, int(ml.get("learning_days", 30)))
    ns = raw.get("night_strategy", {})
    try:
        errors = db.morning_timing_errors(
            now.date(), days, float(ns.get("morning_surplus_threshold_w", 350.0)), int(ns.get("sustained_minutes", 30))
        )
    except Exception:
        errors = []
    if len(errors) < min_days:
        return 0.0, f"learning_wait_{len(errors)}/{min_days}"
    limit = abs(float(ml.get("max_abs_minutes", 45)))
    # night_floor_deadline already applies the manual site/horizon bias. Learn only
    # the residual error so the same horizon delay is not counted twice.
    manual_bias = float(raw.get("pv", {}).get("wakeup_bias_minutes", 0.0))
    residual = statistics.median(errors) - manual_bias
    bias = max(-limit, min(limit, residual))
    return bias, f"median_residual_{len(errors)}d"


def adaptive_night_floor_deadline(raw: Dict[str, Any], db: Any, day: Any, now: dt.datetime) -> Tuple[dt.datetime, float, str]:
    base = night_floor_deadline(raw, day)
    bias, source = learned_morning_bias_minutes(raw, db, now)
    adjusted = base + dt.timedelta(minutes=bias)
    # Never target before sunrise or absurdly late into the morning.
    upper = max(day.sunrise, getattr(day, "useful_pv_start", day.sunrise) + dt.timedelta(minutes=90))
    adjusted = max(day.sunrise, min(upper, adjusted))
    return adjusted, bias, source

def remaining_pv_kwh(day: Any, start_at: dt.datetime) -> float:
    return sum(
        max(0.0, float(p.predicted_w)) / 1000.0 * 0.25
        for p in (getattr(day, "points", []) or [])
        if start_at <= p.time <= day.sunset
    )


def night_export_for_target_w(
    raw: Dict[str, Any],
    current_soc: float,
    now: dt.datetime,
    target_time: dt.datetime,
    target_soc: float,
) -> int:
    """Export cap that should land near target_soc at target_time.

    House base load + inverter overhead are subtracted because they already consume
    battery energy.  If those loads alone are enough to reach the target, return 0W.
    """
    batt_kwh = float(raw.get("battery", {}).get("effective_kwh", 15.0))
    hard = int(raw.get("grid", {}).get("export_hard_limit_w", 1000))
    step = int(raw.get("control", {}).get("write_step_w", 100))
    hours = (target_time - now).total_seconds() / 3600.0
    if hours <= 0.15 or current_soc <= target_soc:
        return 0
    energy_kwh = max(0.0, float(current_soc) - float(target_soc)) / 100.0 * batt_kwh
    battery_w = energy_kwh * 1000.0 / hours
    export_w = (
        battery_w
        - float(raw.get("load_model", {}).get("base_house_load_w", 115.0))
        - float(raw.get("load_model", {}).get("system_overhead_w", 130.0))
    )
    return quantize_export_nearest(export_w, step, hard)


def projected_soc_at_deadline(
    raw: Dict[str, Any],
    current_soc: float,
    now: dt.datetime,
    deadline: dt.datetime,
    export_w: int,
) -> float:
    floor = float(raw.get("battery", {}).get("soc_floor_pct", 15.0))
    batt_kwh = float(raw.get("battery", {}).get("effective_kwh", 15.0))
    hours = max(0.0, (deadline - now).total_seconds() / 3600.0)
    draw_w = (
        max(0.0, float(export_w))
        + float(raw.get("load_model", {}).get("base_house_load_w", 115.0))
        + float(raw.get("load_model", {}).get("system_overhead_w", 130.0))
    )
    delta_pct = (draw_w / 1000.0 * hours) / batt_kwh * 100.0 if batt_kwh > 0 else 0.0
    return max(floor, min(100.0, float(current_soc) - delta_pct))


def _conservative_required_morning_soc(
    raw: Dict[str, Any],
    db: Any,
    day: Any,
    now: dt.datetime,
    deadline: dt.datetime,
) -> float:
    """SOC worth preserving overnight so the safe next-day budget can still hit 96%.

    This is intentionally an autumn/weak-solar rule.  It assumes no daytime export
    while calculating the reserve requirement; if the safe forecast later improves,
    the daytime budget can reopen export automatically.
    """
    floor = float(raw.get("battery", {}).get("soc_floor_pct", 15.0))
    target_soc = float(raw.get("battery", {}).get("day_target_soc_pct", 96.0))
    batt_kwh = float(raw.get("battery", {}).get("effective_kwh", 15.0))
    charge_eff = max(0.50, min(1.0, float(raw.get("day_strategy", {}).get("charge_efficiency", 0.91))))
    policy = active_strategy(raw)
    base_factor, _ = safe_forecast_factor(raw, db, now, getattr(day, "date", now.date()))
    factor = effective_safe_factor(base_factor, policy)
    safe_pv = remaining_pv_kwh(day, deadline) * factor
    hours = max(0.0, (day.sunset - deadline).total_seconds() / 3600.0)
    house_w, _ = learned_house_load_w(raw, db, now)
    house_kwh = house_w / 1000.0 * hours
    overhead_kwh = float(raw.get("load_model", {}).get("system_overhead_w", 130.0)) / 1000.0 * hours
    heater_kwh, _ = heater_remaining_kwh(raw, deadline)
    cooker_kwh, _ = cooker_remaining_kwh(raw, deadline)
    scheduled_kwh = heater_kwh + cooker_kwh
    reserve_kwh, _reserve_src = strategy_reserve_kwh(raw, db, now, policy)
    usable_for_charge = max(0.0, safe_pv - house_kwh - overhead_kwh - scheduled_kwh - reserve_kwh)
    storable_kwh = usable_for_charge * charge_eff
    required = target_soc - (storable_kwh / batt_kwh * 100.0 if batt_kwh > 0 else 0.0)
    return max(floor, min(target_soc, required))


def build_morning_soc_plan(
    raw: Dict[str, Any],
    db: Any,
    day: Any,
    now: dt.datetime,
    current_soc: float,
) -> MorningSocPlan:
    policy = active_strategy(raw)
    floor = float(raw.get("battery", {}).get("soc_floor_pct", 15.0))
    deadline, _morning_bias, _morning_source = adaptive_night_floor_deadline(raw, db, day, now)

    if now >= deadline:
        return MorningSocPlan(policy.tag, deadline, current_soc, current_soc, 0, "deadline_passed")

    if policy.night_mode == "save":
        desired = current_soc
        export_w = 0
        reason = "strategy_save_no_grid_export"
    elif policy.night_mode == "floor":
        desired = floor
        export_w = night_export_for_target_w(raw, current_soc, now, deadline, desired)
        reason = "strategy_floor_15pct"
    elif policy.night_mode == "economic":
        if economic_battery_export_profitable(raw):
            desired = floor
            export_w = night_export_for_target_w(raw, current_soc, now, deadline, desired)
            reason = "economic_battery_export_profitable"
        else:
            desired = _conservative_required_morning_soc(raw, db, day, now, deadline)
            export_w = night_export_for_target_w(raw, current_soc, now, deadline, desired)
            reason = "economic_preserve_for_self_use"
    else:
        desired = _conservative_required_morning_soc(raw, db, day, now, deadline)
        export_w = night_export_for_target_w(raw, current_soc, now, deadline, desired)
        reason = "forecast_protected_morning_reserve"

    projected = projected_soc_at_deadline(raw, current_soc, now, deadline, export_w)
    return MorningSocPlan(policy.tag, deadline, desired, projected, export_w, reason)


def forecast_cumulative_kwh(day: Any, until: dt.datetime, since: Optional[dt.datetime] = None) -> float:
    """Forecast energy over [since, until), from the 15-minute point series.

    Each point carries the energy of the interval it starts, so the upper bound is
    exclusive: integrating to 12:00 must not include the 12:00-12:15 interval. An
    inclusive bound would over-count one interval and bias every ratio low.
    """
    total = 0.0
    for p in (getattr(day, "points", []) or []):
        if since is not None and p.time < since:
            continue
        if p.time >= until:
            break
        total += max(0.0, float(p.predicted_w)) / 1000.0 * 0.25
    return total


def intraday_forecast_bias(
    raw: Dict[str, Any],
    db: Any,
    day: Any,
    now: dt.datetime,
) -> Tuple[float, str]:
    """How today is actually tracking against its own forecast, so far.

    The historical safe factor answers "how wrong is this forecast usually"; it says
    nothing about whether *today* is running above or below its own curve. This
    returns a multiplicative bias correction for the remaining forecast.

    Two rules keep it honest:

    * The forecast is integrated up to the timestamp of the actual sample, never up
      to the wall clock. With stale telemetry the two differ by hours, and comparing
      a partial actual against a full-to-now forecast would invent a fake shortfall.
    * The correction is shrunk toward 1.0 until enough forecast energy has elapsed to
      make the ratio meaningful, so a cloudy 20 minutes after sunrise cannot swing
      the whole afternoon.
    """
    ds = raw.get("day_strategy", {})
    if not bool(ds.get("intraday_bias_enabled", True)):
        return 1.0, "intraday_disabled"
    if db is None or not hasattr(db, "day_production_at"):
        return 1.0, "intraday_unavailable"

    lo = float(ds.get("intraday_bias_min", 0.40))
    hi = float(ds.get("intraday_bias_max", 1.60))
    min_kwh = float(ds.get("intraday_bias_min_forecast_kwh", 1.0))
    trust_kwh = max(0.1, float(ds.get("intraday_bias_trust_kwh", 3.0)))
    window_h = float(ds.get("intraday_bias_window_hours", 3.0))
    date = getattr(day, "date", now.date())

    # Stop at the last sample taken while the array was still unclipped. Past that
    # point measured PV is limited by the inverter rather than the sky, and feeding
    # it back in would read a clipped sunny afternoon as bad weather - which shrinks
    # the budget, closes the export cap further, and clips harder still.
    hysteresis = float(ds.get("intraday_clip_soc_hysteresis_pct", 5.0))
    ceiling = float(ds.get("curtailment_override_soc_pct", 98.0)) - hysteresis
    min_charge = float(ds.get("curtailment_override_charge_w", 300.0))
    clipped_tail = False
    latest = None
    try:
        if hasattr(db, "last_unclipped_production"):
            latest = db.last_unclipped_production(date, ceiling, min_charge)
            newest = db.day_production_at(date)
            if latest and newest and latest[1] < newest[1]:
                clipped_tail = True
        if latest is None:
            latest = db.day_production_at(date)
            clipped_tail = False
    except Exception:
        latest = None
    if not latest:
        return 1.0, "intraday_no_actuals"
    actual_kwh, as_of = latest
    fc_to_now = forecast_cumulative_kwh(day, as_of)

    ratio = None
    source = None
    # Preferred: the trailing window, which reflects conditions right now.
    if window_h > 0:
        since = as_of - dt.timedelta(hours=window_h)
        try:
            earlier = db.day_production_at(date, since)
        except Exception:
            earlier = None
        if earlier:
            prev_kwh, prev_at = earlier
            fc_window = forecast_cumulative_kwh(day, as_of, prev_at)
            act_window = max(0.0, actual_kwh - prev_kwh)
            if fc_window >= min_kwh:
                ratio = act_window / fc_window
                elapsed = fc_window
                source = f"window{window_h:g}h"
    # Fallback: the whole day so far, which is always available once PV has started.
    if ratio is None:
        if fc_to_now < min_kwh:
            return 1.0, f"intraday_wait_{fc_to_now:.1f}/{min_kwh:.1f}kWh"
        ratio = actual_kwh / fc_to_now
        elapsed = fc_to_now
        source = "day_so_far"

    weight = max(0.0, min(1.0, elapsed / trust_kwh))
    shrunk = 1.0 + (ratio - 1.0) * weight
    bias = max(lo, min(hi, shrunk))
    stale_min = (now - as_of).total_seconds() / 60.0
    tag = f"intraday_{source}_r{ratio:.2f}_w{weight:.2f}"
    if clipped_tail:
        tag += "_pre_clipping"
    if stale_min > 30.0:
        tag += f"_stale{stale_min:.0f}min"
    return bias, tag


def strategy_reserve_kwh(
    raw: Dict[str, Any],
    db: Any,
    now: dt.datetime,
    policy: Any,
) -> Tuple[float, str]:
    """The strategy reserve, expressed as hours of measured house draw.

    A fixed slab of kWh is the wrong shape for a reserve: 2.5 kWh is roughly seven
    hours of cover in summer but barely three once heating raises the base load, so
    the same setting is over-cautious in July and thin in January. Scaling it by the
    learned house load keeps the reserve at a constant number of hours of autonomy
    across the season, and it re-tunes itself as the load history moves.
    """
    ds = raw.get("day_strategy", {})
    fixed = float(getattr(policy, "reserve_kwh", 0.0) or 0.0)
    if not bool(ds.get("load_scaled_reserve_enabled", True)):
        return fixed, "fixed"
    hours = float(getattr(policy, "reserve_hours", 0.0) or 0.0)
    if hours <= 0.0:
        return fixed, "fixed_no_hours"
    house_w, house_src = learned_house_load_w(raw, db, now)
    overhead_w = float(raw.get("load_model", {}).get("system_overhead_w", 130.0))
    draw_w = house_w + overhead_w
    kwh = draw_w / 1000.0 * hours
    lo = float(ds.get("reserve_min_kwh", 0.5))
    hi = float(ds.get("reserve_max_kwh", 6.0))
    clamped = max(lo, min(hi, kwh))
    tag = f"load_scaled_{hours:g}h_at_{draw_w:.0f}W_{house_src}"
    if abs(clamped - kwh) > 1e-9:
        tag += f"_clamped_from_{kwh:.2f}"
    return clamped, tag


def surplus_above_cap_kwh(
    raw: Dict[str, Any],
    day: Any,
    start_at: dt.datetime,
    load_w: float,
    factor: float,
) -> float:
    """Forecast energy arriving faster than the export cap can carry it away.

    Export is hard-capped but charging is not: on this class of site the battery
    absorbs several kW while at most one can leave through the meter. Energy above
    ``load + cap`` therefore goes to the battery or is curtailed *whatever the export
    setpoint is*, so it refills the battery without competing with export.

    Treating the refill as something export must be sacrificed for is the mistake
    this corrects: it closes the valve on exactly the days with the most to send.
    """
    hard = float(raw.get("grid", {}).get("export_hard_limit_w", 1000))
    max_charge_w = float(raw.get("analytics", {}).get("battery_max_charge_w", 10000.0))
    total = 0.0
    for p in (getattr(day, "points", []) or []):
        if p.time < start_at or p.time > day.sunset:
            continue
        watts = max(0.0, float(p.predicted_w)) * factor
        # Only what the battery could actually take; the rest is curtailed anyway.
        absorbed = min(max(0.0, watts - load_w - hard), max_charge_w)
        total += absorbed / 1000.0 * 0.25
    return total


def _discharge_efficiency(raw: Dict[str, Any]) -> float:
    return max(0.50, min(1.0, float(raw.get("analytics", {}).get("battery_discharge_efficiency", 0.95))))


def night_energy_need_kwh(
    raw: Dict[str, Any],
    db: Any,
    day: Any,
    tomorrow: Any,
    now: dt.datetime,
) -> Tuple[float, float]:
    """Battery energy needed from sunset until tomorrow's morning PV handoff.

    Returned as battery-stored kWh (house/inverter draw grossed up by discharge
    efficiency), together with the length of that night window in hours.
    """
    house_w, _ = learned_house_load_w(raw, db, now)
    overhead_w = float(raw.get("load_model", {}).get("system_overhead_w", 130.0))
    if tomorrow is not None and hasattr(tomorrow, "sunrise"):
        handoff = night_floor_deadline(raw, tomorrow)
    else:
        fallback = float(raw.get("night_strategy", {}).get("fallback_night_hours", 12.0))
        handoff = day.sunset + dt.timedelta(hours=fallback)
    hours = max(0.0, (handoff - day.sunset).total_seconds() / 3600.0)
    need = (house_w + overhead_w) / 1000.0 * hours / _discharge_efficiency(raw)
    return need, hours


def tomorrow_refill_possible(
    raw: Dict[str, Any],
    db: Any,
    tomorrow: Any,
    now: dt.datetime,
    from_soc: float,
    to_soc: float,
) -> bool:
    """Whether tomorrow's safe PV budget can charge from_soc back up to to_soc.

    Uses the same discounted forecast the daytime budget uses, so a strategy that
    distrusts the forecast also distrusts this release decision.
    """
    if tomorrow is None or not hasattr(tomorrow, "sunset"):
        return False
    if to_soc <= from_soc:
        return True
    batt_kwh = float(raw.get("battery", {}).get("effective_kwh", 15.0))
    if batt_kwh <= 0:
        return False
    charge_eff = max(0.50, min(1.0, float(raw.get("day_strategy", {}).get("charge_efficiency", 0.91))))
    policy = active_strategy(raw)
    base_factor, _ = safe_forecast_factor(raw, db, now, getattr(tomorrow, "date", now.date()))
    factor = effective_safe_factor(base_factor, policy)
    start = night_floor_deadline(raw, tomorrow)
    safe_pv = remaining_pv_kwh(tomorrow, start) * factor
    hours = max(0.0, (tomorrow.sunset - start).total_seconds() / 3600.0)
    house_w, _ = learned_house_load_w(raw, db, now)
    overhead_w = float(raw.get("load_model", {}).get("system_overhead_w", 130.0))
    loads = (house_w + overhead_w) / 1000.0 * hours + scheduled_loads_daily_energy_kwh(raw)
    need_input = (to_soc - from_soc) / 100.0 * batt_kwh / charge_eff
    return (safe_pv - loads) >= need_input


def build_day_energy_plan(
    raw: Dict[str, Any],
    db: Any,
    day: Any,
    now: dt.datetime,
    soc: float,
    tomorrow: Any = None,
) -> DayEnergyPlan:
    policy = active_strategy(raw)
    hard = int(raw.get("grid", {}).get("export_hard_limit_w", 1000))
    step = int(raw.get("control", {}).get("write_step_w", 100))
    batt_kwh = float(raw.get("battery", {}).get("effective_kwh", 15.0))
    charge_eff = max(0.50, min(1.0, float(raw.get("day_strategy", {}).get("charge_efficiency", 0.91))))
    target = battery_target(raw, db, now)

    if hasattr(day, "sunrise"):
        deadline, _morning_bias, _morning_source = adaptive_night_floor_deadline(raw, db, day, now)
    else:
        deadline = now
    morning_desired = None
    morning_projected = None
    if now < deadline:
        morning = build_morning_soc_plan(raw, db, day, now, soc)
        deadline = morning.deadline
        start_at = deadline
        planning_soc = morning.projected_soc_pct
        morning_desired = morning.desired_soc_pct
        morning_projected = morning.projected_soc_pct
    else:
        start_at = now
        planning_soc = float(soc)

    raw_pv = remaining_pv_kwh(day, start_at)
    base_factor, factor_source = safe_forecast_factor(raw, db, now, getattr(day, "date", now.date()))
    distribution, distribution_source = forecast_distribution(raw, db, now, getattr(day, "date", now.date()))
    safe_factor = effective_safe_factor(base_factor, policy)
    if abs(safe_factor - base_factor) > 1e-9:
        factor_source = f"{factor_source}+strategy_{policy.tag}"
    # The intraday bias corrects the forecast itself; the safe factor remains a
    # separate uncertainty discount applied on top, so correcting for today's
    # conditions never spends the safety margin.
    bias, bias_source = intraday_forecast_bias(raw, db, day, now)
    corrected_pv = raw_pv * bias
    safe_pv = corrected_pv * safe_factor
    house_w, house_source = learned_house_load_w(raw, db, now)
    hours = max(0.0, (day.sunset - start_at).total_seconds() / 3600.0)
    house_kwh = house_w / 1000.0 * hours
    overhead_w = float(raw.get("load_model", {}).get("system_overhead_w", 130.0))
    overhead_kwh = overhead_w / 1000.0 * hours
    heater_kwh, heater_status = heater_remaining_kwh(raw, start_at)
    cooker_kwh, cooker_status = cooker_remaining_kwh(raw, start_at)
    scheduled_kwh = heater_kwh + cooker_kwh
    reserve, reserve_source = strategy_reserve_kwh(raw, db, now, policy)

    selected_target_soc = target.target_soc_pct
    target_reason = target.reason
    if target.maintenance_due:
        normal_target = float(raw.get("battery", {}).get("day_target_soc_pct", 96.0))
        maintenance_stored = max(0.0, target.target_soc_pct - planning_soc) / 100.0 * batt_kwh
        maintenance_input = maintenance_stored / charge_eff if maintenance_stored > 0 else 0.0
        non_export_available = safe_pv - house_kwh - overhead_kwh - scheduled_kwh
        if non_export_available < maintenance_input + reserve:
            selected_target_soc = normal_target
            target_reason = "maintenance_deferred_forecast"

    # End-of-day SOC requirement.
    #
    # The strategy reserve is applied here, as a floor under the energy the battery
    # must still hold at sunset, instead of being skimmed off the export budget.
    # Subtracting it from export as well double-counts it: at high SOC the reserve
    # is already physically stored, and skimming it again suppresses export while
    # the battery is full and surplus PV is being curtailed.
    floor_soc = float(raw.get("battery", {}).get("soc_floor_pct", 15.0))
    discharge_eff = _discharge_efficiency(raw)
    night_need, night_hours = night_energy_need_kwh(raw, db, day, tomorrow, now)
    if batt_kwh > 0:
        required_end_soc = floor_soc + (night_need + reserve) / batt_kwh * 100.0
    else:
        required_end_soc = selected_target_soc
    required_end_soc = max(floor_soc, min(100.0, required_end_soc))

    if tomorrow_refill_possible(raw, db, tomorrow, now, required_end_soc, selected_target_soc):
        eod_target_soc = required_end_soc
        eod_reason = "release_stored_surplus_forecast_refills"
    else:
        eod_target_soc = max(required_end_soc, selected_target_soc)
        eod_reason = "hold_day_target_forecast_cannot_refill"

    stored_need = max(0.0, eod_target_soc - planning_soc) / 100.0 * batt_kwh
    input_need = stored_need / charge_eff if stored_need > 0 else 0.0

    # Energy balance to sunset. Stored energy above the end-of-day requirement is
    # exportable; a shortfall must be charged, but only the part of it that export
    # actually competes for.
    pv_surplus = safe_pv - house_kwh - overhead_kwh - scheduled_kwh
    stored_delta = (planning_soc - eod_target_soc) / 100.0 * batt_kwh
    above_cap_kwh = 0.0
    deficit_from_above_cap = 0.0
    if stored_delta >= 0:
        stored_surplus = stored_delta * discharge_eff
        exportable = pv_surplus + stored_surplus
    else:
        stored_surplus = 0.0
        if bool(raw.get("day_strategy", {}).get("above_cap_refill_enabled", True)):
            above_cap_kwh = surplus_above_cap_kwh(
                raw, day, start_at, house_w + overhead_w, safe_factor * bias
            )
            # The battery refills from energy the cap cannot carry regardless, so
            # only the shortfall beyond that has to be taken out of export.
            deficit_from_above_cap = min(above_cap_kwh, input_need)
        remaining_deficit = max(0.0, input_need - deficit_from_above_cap)
        exportable = pv_surplus - remaining_deficit
    available_for_export = max(0.0, exportable)

    # Forced export: surplus the battery has no room left to take.
    #
    # Holding the cap back only preserves energy if there is somewhere to preserve it.
    # Once the remaining PV surplus exceeds the battery's remaining headroom, the
    # excess leaves through the meter or is thrown away - conserving it is not one of
    # the options. Near a full battery this is what keeps the cap open.
    headroom_input_kwh = max(0.0, (100.0 - planning_soc) / 100.0 * batt_kwh) / charge_eff
    forced_export_kwh = max(0.0, pv_surplus - headroom_input_kwh)
    if forced_export_kwh > available_for_export:
        available_for_export = forced_export_kwh

    full_export_energy = hard / 1000.0 * hours
    full_margin = available_for_export - full_export_energy
    average_budget_w = (available_for_export / hours * 1000.0) if hours > 0 else 0.0
    # Hours the budget could sustain export at the hard cap. Export is cap-limited,
    # so headroom left unused in one interval is lost for good; spreading the budget
    # as a flat average never reaches the cap even when the budget can sustain it.
    cap_sustain_hours = (available_for_export / (hard / 1000.0)) if hard > 0 else 0.0

    # Staleness-robust floor.
    #
    # A setpoint is not a decision that can be revised next minute: cloud telemetry
    # routinely goes stale for hours, and writes are frozen while it is, so whatever
    # is written may stand until sunset. That makes zero the worst available choice -
    # harmless if revisable, and a whole afternoon of lost export if it is not.
    #
    # So during daylight, floor the recommendation at a modest export, but only when
    # the discounted forecast shows the end-of-day requirement is still met after
    # paying for it. If the day cannot afford the floor, charging still wins.
    ds = raw.get("day_strategy", {})
    floor_w = int(ds.get("staleness_floor_w", 300))
    floor_applied = False
    floor_reason = "disabled"
    if not bool(ds.get("staleness_floor_enabled", True)) or floor_w <= 0:
        floor_w = 0
    elif hours <= 0.5:
        floor_w, floor_reason = 0, "window_closing"
    else:
        floor_cost = floor_w / 1000.0 * hours
        net_charge = max(0.0, safe_pv - house_kwh - overhead_kwh - scheduled_kwh - floor_cost)
        projected_soc = min(100.0, planning_soc + net_charge * charge_eff / batt_kwh * 100.0) \
            if batt_kwh > 0 else planning_soc
        if projected_soc >= required_end_soc:
            floor_applied = True
            floor_reason = f"projected_{projected_soc:.0f}pct_ge_required_{required_end_soc:.0f}pct"
        else:
            floor_w = 0
            floor_reason = f"projected_{projected_soc:.0f}pct_below_required_{required_end_soc:.0f}pct"

    def _allocate() -> Tuple[int, str]:
        if hours <= 0.01:
            return 0, "window_closed"
        if cap_sustain_hours >= hours - 0.01:
            return hard, "cap_sustained"
        budget_w = quantize_export_floor(average_budget_w, step, hard)
        if floor_applied and budget_w < floor_w:
            return min(hard, floor_w), "staleness_floor"
        return budget_w, "flat_average"

    if policy.day_mode == "full_export":
        recommended, allocation = (hard, "strategy_full_export") if hours > 0.01 else (0, "window_closed")
    elif policy.day_mode == "save":
        recommended, allocation = 0, "strategy_save"
    elif policy.day_mode == "economic":
        if economic_battery_export_profitable(raw) and hours > 0.01:
            recommended, allocation = hard, "economic_export_profitable"
        else:
            recommended, allocation = _allocate()
    else:
        recommended, allocation = _allocate()

    return DayEnergyPlan(
        strategy_tag=policy.tag,
        planning_start_at=start_at.isoformat(),
        planning_soc_pct=planning_soc,
        morning_desired_soc_pct=morning_desired,
        morning_projected_soc_pct=morning_projected,
        target_soc_pct=selected_target_soc,
        target_reason=target_reason,
        maintenance_due=target.maintenance_due,
        last_full_at=target.last_full_at.isoformat() if target.last_full_at else None,
        raw_remaining_pv_kwh=raw_pv,
        base_safe_forecast_factor=base_factor,
        safe_forecast_factor=safe_factor,
        intraday_bias=bias,
        intraday_bias_source=bias_source,
        bias_corrected_pv_kwh=corrected_pv,
        safe_remaining_pv_kwh=safe_pv,
        forecast_factor_source=factor_source,
        forecast_distribution=distribution,
        forecast_distribution_source=distribution_source,
        house_load_w=house_w,
        house_load_source=house_source,
        house_energy_kwh=house_kwh,
        system_overhead_kwh=overhead_kwh,
        water_heater_kwh=heater_kwh,
        water_heater_status=heater_status,
        cooker_kwh=cooker_kwh,
        cooker_status=cooker_status,
        scheduled_loads_kwh=scheduled_kwh,
        battery_stored_kwh_needed=stored_need,
        battery_input_kwh_needed=input_need,
        reserve_kwh=reserve,
        reserve_source=reserve_source,
        end_of_day_target_soc_pct=eod_target_soc,
        end_of_day_target_reason=eod_reason,
        night_energy_need_kwh=night_need,
        night_hours=night_hours,
        pv_surplus_kwh=pv_surplus,
        stored_surplus_kwh=stored_surplus,
        surplus_above_cap_kwh=above_cap_kwh,
        deficit_covered_by_above_cap_kwh=deficit_from_above_cap,
        battery_headroom_kwh=headroom_input_kwh,
        forced_export_kwh=forced_export_kwh,
        export_energy_budget_kwh=available_for_export,
        hours_to_sunset=hours,
        cap_sustain_hours=cap_sustain_hours,
        staleness_floor_w=int(floor_w if floor_applied else 0),
        staleness_floor_reason=floor_reason,
        full_export_margin_kwh=full_margin,
        recommended_export_w=recommended,
        allocation_mode=allocation,
    )
