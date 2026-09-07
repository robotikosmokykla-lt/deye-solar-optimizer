#!/usr/bin/env python3
"""Deye Solar Optimizer v3.1.0: forecast-aware, low-write-count controller.

Design rules:
- /device/latest is the primary telemetry source; collectionTime is its freshness clock.
- The actual write endpoint is also the online probe: no preliminary dynamic-read
  command is issued, avoiding Deye command-queue contention (2104004).
- Explicit OFFLINE/BUSY rejections are not accepted writes and are retried later.
- Once Deye returns a positive orderId, never duplicate that command while pending.
- Ambiguous transport/submission failures trigger a conservative no-retry guard.
- The default write endpoint is the narrow MAX_SELL_POWER endpoint.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import signal
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from deye_api import (
    DeyeAPIError,
    flatten_device_latest,
    is_busy_error,
    is_offline_error,
    parse_deye_timestamp,
)
from solar_forecast import DayForecast, ForecastError, PVArray, fetch_forecast
from energy_strategy import (
    battery_target,
    build_day_energy_plan,
    build_morning_soc_plan,
    night_export_for_target_w,
    night_floor_deadline,
)
from strategy_presets import active_strategy
from config_loader import DEFAULT_ENV, load_config as load_env_config, make_deye_client
from state_db import StateDB


@dataclass
class Config:
    raw: Dict[str, Any]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.raw["site"]["timezone"])


def cget(cfg: Config, section: str, key: str, default: Any) -> Any:
    return cfg.raw.get(section, {}).get(key, default)


def load_config(path: str) -> Config:
    return Config(load_env_config(path))


class EventLog:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def emit(self, level: str, event: str, **fields: Any) -> None:
        rec = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "level": level,
            "event": event,
            **fields,
        }
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        print(line, flush=True)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:
            print(
                json.dumps({"level": "ERROR", "event": "jsonl_write_failed", "error": str(exc)}),
                file=sys.stderr,
                flush=True,
            )


def clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def as_int(v: Any) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def quantize_w(v: float, step: int, hard_limit: int) -> int:
    if step <= 0:
        step = 100
    q = int(round(v / step) * step)
    return int(clamp(q, 0, hard_limit))


def calculate_night_export_w(
    cfg: Config,
    current_soc: float,
    now: dt.datetime,
    target_time: dt.datetime,
    target_soc: Optional[float] = None,
) -> int:
    if target_soc is None:
        target_soc = float(cfg.raw["battery"]["soc_floor_pct"])
    return night_export_for_target_w(cfg.raw, current_soc, now, target_time, float(target_soc))


def expected_soc_on_linear_plan(
    start_soc: float,
    target_soc: float,
    start: dt.datetime,
    target: dt.datetime,
    now: dt.datetime,
) -> float:
    total = (target - start).total_seconds()
    if total <= 0:
        return target_soc
    remaining = clamp((target - now).total_seconds() / total, 0.0, 1.0)
    return target_soc + (start_soc - target_soc) * remaining


def control_pv_wakeup(cfg: Config, forecast: DayForecast) -> dt.datetime:
    """SOC-floor deadline based on sustained useful morning PV.

    Retained under the old function name for compatibility with status/tests. v3.1.0
    no longer targets sunrise+site-bias; it aims for the configured sustained-PV
    threshold with a small lead.
    """
    return night_floor_deadline(cfg.raw, forecast)


@dataclass
class DeviceSnapshot:
    collection_at: Optional[dt.datetime]
    device_state: Optional[int]
    metrics: Dict[str, Any]
    raw_response: Dict[str, Any]

    @property
    def raw_soc(self) -> Optional[float]:
        return num(self.metrics.get("SOC"))

    @property
    def battery_power_w(self) -> Optional[float]:
        return num(self.metrics.get("BatteryPower"))

    @property
    def grid_power_w(self) -> Optional[float]:
        return num(self.metrics.get("TotalGridPower"))

    @property
    def consumption_power_w(self) -> Optional[float]:
        return num(self.metrics.get("TotalConsumptionPower"))

    @property
    def solar_power_w(self) -> Optional[float]:
        return num(self.metrics.get("TotalSolarPower"))


def parse_device_snapshot(data: Dict[str, Any], tz: dt.tzinfo) -> DeviceSnapshot:
    flat = flatten_device_latest(data)
    return DeviceSnapshot(
        collection_at=parse_deye_timestamp(flat.get("collectionTime"), tz),
        device_state=as_int(flat.get("deviceState")),
        metrics=dict(flat.get("metrics") or {}),
        raw_response=data,
    )


def derive_control_soc(
    cfg: Config,
    snap: DeviceSnapshot,
    now: dt.datetime,
    *,
    allow_power_extrapolation: bool = True,
) -> Tuple[Optional[float], str, Optional[float]]:
    """Return (SOC used for control, confidence, telemetry age minutes).

    Deye Cloud can hold device/latest unchanged for tens of minutes. At night, when
    PV is near zero and BatteryPower is available, extrapolate SOC using the last
    measured battery power. Validated against overnight discharges: the estimate
    tracked the real SOC drop to within a few tenths of a percentage point over
    intervals approaching an hour.
    """
    raw_soc = snap.raw_soc
    if raw_soc is None or not 0 <= raw_soc <= 100 or snap.collection_at is None:
        return raw_soc, "INVALID", None
    age_min = max(0.0, (now - snap.collection_at).total_seconds() / 60.0)
    fresh_min = float(cget(cfg, "control", "soc_fresh_minutes", 5))
    estimate_max_min = float(cget(cfg, "control", "soc_estimate_max_minutes", 75))
    estimate_max_pv = float(cget(cfg, "control", "soc_estimate_max_pv_w", 100))
    if age_min <= fresh_min:
        return raw_soc, "FRESH", age_min

    bp = snap.battery_power_w
    pv = snap.solar_power_w
    if (
        allow_power_extrapolation
        and age_min <= estimate_max_min
        and bp is not None
        and abs(bp) <= 20000
        and (pv is None or abs(pv) <= estimate_max_pv)
    ):
        kwh = float(cfg.raw["battery"]["effective_kwh"])
        delta_pct = bp * (age_min / 60.0) / (kwh * 1000.0) * 100.0
        max_delta = float(cget(cfg, "control", "soc_estimate_max_delta_pct", 15.0))
        delta_pct = clamp(delta_pct, -max_delta, max_delta)
        estimated = clamp(raw_soc - delta_pct, 0.0, 100.0)
        return estimated, "ESTIMATED", age_min

    max_age = float(cget(cfg, "control", "max_control_telemetry_age_minutes", 90))
    if age_min <= max_age:
        return raw_soc, "STALE", age_min
    return raw_soc, "TOO_OLD", age_min


def control_response_accepted(response: Dict[str, Any]) -> Tuple[bool, Optional[int], str]:
    """Interpret DeviceControlResponse without confusing cloud success with device acceptance."""
    connection = as_int(response.get("connectionStatus"))
    order_id = as_int(response.get("orderId"))
    if connection == 0:
        return False, None, "device_offline"
    if order_id is None or order_id <= 0:
        return False, None, "no_order_id"
    return True, order_id, "accepted"


class Controller:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        r = cfg.raw
        self.tz = cfg.tz
        self.db = StateDB(r["logging"]["state_db"])
        self.log = EventLog(r["logging"]["jsonl_log"])
        self.client = make_deye_client(r)
        self.forecasts: Dict[dt.date, DayForecast] = {}
        self.forecast_fetched_at: Optional[dt.datetime] = None
        self.current_setting_w: Optional[int] = self.db.get("last_known_setting_w")
        self.last_collection_at: Optional[dt.datetime] = None
        stored = self.db.get("last_device_collection_at")
        if stored:
            try:
                self.last_collection_at = dt.datetime.fromisoformat(stored)
            except Exception:
                pass
        self.next_control_attempt_at: Optional[dt.datetime] = None
        self.next_order_poll_at: Optional[dt.datetime] = None
        self.active_order = self.db.last_pending_write()
        self.active_context: Dict[str, Any] = self.db.get("active_order_context", {}) or {}
        self.stop = False

    def close(self) -> None:
        self.db.close()

    def refresh_forecast(self, now: dt.datetime, force: bool = False) -> None:
        minutes = int(cget(self.cfg, "control", "forecast_refresh_minutes", 60))
        if not force and self.forecast_fetched_at and (now - self.forecast_fetched_at).total_seconds() < minutes * 60:
            return
        arrays = [
            PVArray(a["name"], float(a["kwp"]), float(a["tilt_deg"]), float(a["azimuth_deg"]))
            for a in self.cfg.raw["pv"]["arrays"]
        ]
        try:
            fc = fetch_forecast(
                latitude=float(self.cfg.raw["site"]["latitude"]),
                longitude=float(self.cfg.raw["site"]["longitude"]),
                timezone=self.cfg.raw["site"]["timezone"],
                arrays=arrays,
                performance_ratio=float(self.cfg.raw["pv"]["performance_ratio"]),
                wake_threshold_w=float(self.cfg.raw["pv"]["wake_threshold_w"]),
                useful_threshold_w=float(self.cfg.raw["pv"]["useful_threshold_w"]),
                wakeup_bias_minutes=int(self.cfg.raw["pv"].get("wakeup_bias_minutes", 0)),
                forecast_days=3,
            )
        except ForecastError as exc:
            self.db.add_api_event(now, "open_meteo", False, str(exc))
            self.log.emit("WARN", "forecast_failed", error=str(exc))
            return
        self.forecasts = fc
        self.forecast_fetched_at = now
        for f in fc.values():
            self.db.add_forecast(f)
        self.db.add_api_event(now, "open_meteo", True, f"days={len(fc)}")
        tomorrow = fc.get(now.date() + dt.timedelta(days=1))
        self.log.emit(
            "INFO",
            "forecast_refreshed",
            days=len(fc),
            tomorrow_control_wakeup=control_pv_wakeup(self.cfg, tomorrow).isoformat() if tomorrow else None,
            tomorrow_forecast_wakeup=tomorrow.pv_wakeup.isoformat() if tomorrow else None,
            tomorrow_expected_kwh=round(tomorrow.expected_kwh, 2) if tomorrow else None,
        )

    def get_forecast(self, day: dt.date) -> Optional[DayForecast]:
        return self.forecasts.get(day)

    def fetch_device(self, now: dt.datetime) -> Optional[DeviceSnapshot]:
        try:
            data = self.client.get_device_latest(str(self.cfg.raw["deye"]["inverter_sn"]))
            snap = parse_device_snapshot(data, self.tz)
            self.db.add_api_event(now, "device_latest", True, "ok")
            return snap
        except DeyeAPIError as exc:
            self.db.add_api_event(now, "device_latest", False, str(exc))
            self.log.emit("ERROR", "telemetry_failed", error=str(exc))
            return None

    def persist_snapshot(
        self,
        now: dt.datetime,
        snap: DeviceSnapshot,
        control_soc: Optional[float],
        confidence: str,
        age_min: Optional[float],
    ) -> bool:
        fresh = bool(
            snap.collection_at
            and (self.last_collection_at is None or snap.collection_at > self.last_collection_at)
        )
        self.db.set("current_soc_raw", snap.raw_soc, now)
        self.db.set("current_soc", control_soc, now)
        self.db.set("soc_confidence", confidence, now)
        self.db.set("telemetry_age_minutes", age_min, now)
        self.db.set("device_state", snap.device_state, now)
        if fresh and snap.collection_at:
            self.last_collection_at = snap.collection_at
            self.db.set("last_device_collection_at", snap.collection_at.isoformat(), now)
            vals = dict(snap.metrics)
            vals["__device_state"] = snap.device_state
            self.db.add_device_telemetry(
                now,
                snap.collection_at,
                vals,
                control_soc=control_soc,
                soc_confidence=confidence,
                telemetry_age_minutes=age_min,
                raw_json=snap.raw_response,
            )
            full_threshold = float(cget(self.cfg, "maintenance", "full_soc_threshold_pct", 99.5))
            if snap.raw_soc is not None and snap.raw_soc >= full_threshold:
                self.db.set("last_full_balance_at", snap.collection_at.isoformat(), now)
        return fresh

    def update_telemetry_health(
        self,
        now: dt.datetime,
        snap: DeviceSnapshot,
        age_min: Optional[float],
        new_sample: bool,
    ) -> str:
        """Track DeyeCloud telemetry stalls independently of HTTP/API success."""
        warn_min = float(cget(self.cfg, "telemetry_health", "stale_warning_minutes", 10))
        offline_min = float(cget(self.cfg, "telemetry_health", "cloud_offline_minutes", 20))
        if age_min is None:
            candidate = "UNKNOWN"
        elif age_min > offline_min:
            candidate = "CLOUD_OFFLINE"
        elif age_min > warn_min:
            candidate = "STALE"
        else:
            candidate = "FRESH"

        previous = str(self.db.get("telemetry_health_state", "UNKNOWN"))
        state = candidate
        if previous == "CLOUD_OFFLINE" and candidate == "FRESH" and new_sample:
            state = "RECOVERED"
            started_text = self.db.get("telemetry_stall_started_at")
            duration_min = None
            if started_text:
                try:
                    started = dt.datetime.fromisoformat(str(started_text))
                    duration_min = (now - started).total_seconds() / 60.0
                except Exception:
                    pass
            self.db.set("telemetry_last_recovered_at", now.isoformat(), now)
            self.db.set("telemetry_last_stall_duration_min", duration_min, now)
            self.log.emit(
                "INFO",
                "telemetry_recovered",
                duration_min=round(duration_min, 1) if duration_min is not None else None,
                collection_at=snap.collection_at.isoformat() if snap.collection_at else None,
                device_state=snap.device_state,
            )
        elif previous == "RECOVERED" and candidate == "FRESH":
            state = "FRESH"

        if state != previous:
            self.db.set("telemetry_health_state", state, now)
            self.log.emit(
                "WARN" if state in {"STALE", "CLOUD_OFFLINE"} else "INFO",
                "telemetry_state_changed",
                previous=previous,
                state=state,
                age_min=round(age_min, 1) if age_min is not None else None,
                collection_at=snap.collection_at.isoformat() if snap.collection_at else None,
                device_state=snap.device_state,
            )

        if state == "CLOUD_OFFLINE" and previous != "CLOUD_OFFLINE":
            self.db.set("telemetry_stall_started_at", now.isoformat(), now)
            incident = {
                "started_at": now.isoformat(),
                "last_collection_at": snap.collection_at.isoformat() if snap.collection_at else None,
                "device_state": snap.device_state,
                "soc": snap.raw_soc,
                "pv_w": snap.solar_power_w,
                "battery_w": snap.battery_power_w,
                "grid_w": snap.grid_power_w,
                "load_w": snap.consumption_power_w,
                "ac_temperature_c": num(snap.metrics.get("AC Temperature")),
            }
            self.db.set("telemetry_stall_incident", incident, now)
            self.log.emit("ERROR", "telemetry_stall_started", **incident)
        return state

    def local_operating_profile(self) -> Optional[str]:
        """Return the last explicitly applied seasonal profile, if any.

        The profile manager writes this state only after Deye confirms the requested
        profile order(s).  This is intentionally local state, not a guessed mapping
        from cached telemetry.  In self-consumption mode the normal controller must
        not keep trying to schedule overnight grid export.
        """
        path = Path(self.cfg.raw["logging"]["state_db"]).parent / "profile-state.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = str(data.get("profile", "")).strip().lower()
            if value in {"export-first", "self-consumption"}:
                return value
        except Exception:
            pass
        return None

    def curtailment_override(
        self,
        now: dt.datetime,
        snap: Optional[DeviceSnapshot],
        recommended: Optional[int],
        day_export: int,
    ) -> Tuple[Optional[int], str]:
        """Reopen the sell cap when the battery is full and PV is being throttled.

        The daytime budget is feed-forward from a discounted forecast. When the
        battery has reached its ceiling and is no longer absorbing, PV that is not
        exported in this interval is curtailed and lost for good, so no forecast
        conservatism can justify holding the cap below the legal limit here.
        Requires fresh telemetry: a stale snapshot must never open the valve.
        """
        if snap is None or not bool(cget(self.cfg, "day_strategy", "curtailment_override_enabled", True)):
            return None, ""
        if snap.collection_at is None:
            return None, ""
        max_age = float(cget(self.cfg, "day_strategy", "max_telemetry_age_minutes", 15.0))
        if (now - snap.collection_at).total_seconds() / 60.0 > max_age:
            return None, ""
        soc = num(snap.raw_soc)
        pv_w = num(snap.solar_power_w)
        battery_w = num(snap.battery_power_w)
        if soc is None or pv_w is None or battery_w is None:
            return None, ""
        soc_threshold = float(cget(self.cfg, "day_strategy", "curtailment_override_soc_pct", 98.0))
        charge_ceiling = float(cget(self.cfg, "day_strategy", "curtailment_override_charge_w", 300.0))
        min_pv_w = float(cget(self.cfg, "day_strategy", "curtailment_override_min_pv_w", 100.0))
        charge_w = max(0.0, -float(battery_w))  # negative battery power == charging
        if float(soc) < soc_threshold or charge_w > charge_ceiling or float(pv_w) < min_pv_w:
            return None, ""
        if recommended is not None and int(recommended) >= int(day_export):
            return None, ""
        return int(day_export), "curtailment_override_battery_full"

    def classify_and_decide(
        self,
        now: dt.datetime,
        soc: float,
        snap: Optional[DeviceSnapshot] = None,
    ) -> Tuple[str, Optional[int], Optional[dt.datetime], str]:
        day_export = int(self.cfg.raw["grid"]["day_export_w"])
        policy = active_strategy(self.cfg.raw)
        if hasattr(self, "db") and self.db is not None:
            self.db.set("active_strategy", policy.tag, now)
        profile = self.local_operating_profile()
        if profile == "self-consumption":
            # A manually applied Deye self-consumption profile remains authoritative.
            # The SAVE strategy also closes the sell cap; other tags retain the legal
            # ceiling for genuine PV surplus but do not force night export here.
            requested = 0 if policy.day_mode == "save" else day_export
            return "SELF_CONSUMPTION", requested, None, "profile_self_consumption"

        today = self.get_forecast(now.date())
        tomorrow = self.get_forecast(now.date() + dt.timedelta(days=1))
        floor_soc = float(self.cfg.raw["battery"]["soc_floor_pct"])

        if today:
            today_floor_deadline = control_pv_wakeup(self.cfg, today)
            night_start = today.sunset - dt.timedelta(
                minutes=int(cget(self.cfg, "control", "night_start_minutes_before_sunset", 30))
            )

            if today_floor_deadline <= now < night_start:
                recommended = day_export
                reason = "morning_day_restore"
                if policy.day_mode == "save":
                    recommended = 0
                    reason = "strategy_save_day"
                elif policy.day_mode == "full_export":
                    recommended = day_export
                    reason = "strategy_max_export_day"
                elif bool(cget(self.cfg, "day_strategy", "auto_export_budget_control", True)):
                    try:
                        plan = build_day_energy_plan(self.cfg.raw, self.db, today, now, soc, tomorrow)
                        recommended = min(day_export, int(plan.recommended_export_w))
                        self.db.set("day_energy_plan", plan.as_dict(), now)
                        self.db.set("current_day_target_soc", plan.target_soc_pct, now)
                        self.db.set("maintenance_due", plan.maintenance_due, now)
                        morning_window = today_floor_deadline + dt.timedelta(
                            minutes=int(cget(self.cfg, "day_strategy", "morning_restore_window_minutes", 90))
                        )
                        reason = "morning_day_restore" if now < morning_window else "day_energy_budget"
                    except Exception as exc:
                        self.log.emit("WARN", "day_energy_plan_failed", error=str(exc), strategy=policy.tag)
                        # Conservative means fail closed on export. Risky explicitly
                        # accepts the opposite behavior.
                        recommended = 0 if policy.tag in {"conservative", "save"} else day_export
                        reason = "day_plan_failed_conservative" if recommended == 0 else "day_plan_failed_risky"
                if policy.day_mode != "save":
                    override_w, override_reason = self.curtailment_override(now, snap, recommended, day_export)
                    if override_w is not None:
                        self.log.emit(
                            "WARN",
                            "curtailment_override",
                            planned_w=recommended,
                            override_w=override_w,
                            soc=num(snap.raw_soc),
                            pv_w=num(snap.solar_power_w),
                            battery_w=num(snap.battery_power_w),
                        )
                        recommended, reason = override_w, override_reason
                return "DAY", recommended, None, reason

            if now >= night_start:
                if tomorrow is not None:
                    morning = build_morning_soc_plan(self.cfg.raw, self.db, tomorrow, now, soc)
                    self.db.set("morning_soc_plan", {
                        "strategy": morning.strategy_tag,
                        "deadline": morning.deadline.isoformat(),
                        "desired_soc_pct": morning.desired_soc_pct,
                        "projected_soc_pct": morning.projected_soc_pct,
                        "export_w": morning.export_w,
                        "reason": morning.reason,
                    }, now)
                    self.db.set("current_night_target_soc", morning.desired_soc_pct, now)
                    if policy.night_mode == "save":
                        return "NIGHT", 0, morning.deadline, "strategy_save_night"
                    if soc <= floor_soc + 0.5 and morning.desired_soc_pct <= floor_soc + 0.5:
                        return "NIGHT", None, morning.deadline, "soc_floor_reached"
                    return "NIGHT", morning.export_w, morning.deadline, morning.reason
                if policy.tag in {"conservative", "save"}:
                    self.db.set("current_night_target_soc", soc, now)
                    return "NIGHT", 0, now + dt.timedelta(hours=10), "forecast_missing_preserve_battery"
                target = now + dt.timedelta(hours=10)
                self.db.set("current_night_target_soc", floor_soc, now)
                return "NIGHT", calculate_night_export_w(self.cfg, soc, now, target, floor_soc), target, "night_plan_fallback"

            if now < today_floor_deadline:
                morning = build_morning_soc_plan(self.cfg.raw, self.db, today, now, soc)
                self.db.set("morning_soc_plan", {
                    "strategy": morning.strategy_tag,
                    "deadline": morning.deadline.isoformat(),
                    "desired_soc_pct": morning.desired_soc_pct,
                    "projected_soc_pct": morning.projected_soc_pct,
                    "export_w": morning.export_w,
                    "reason": morning.reason,
                }, now)
                self.db.set("current_night_target_soc", morning.desired_soc_pct, now)
                if policy.night_mode == "save":
                    return "NIGHT", 0, morning.deadline, "strategy_save_night"
                if soc <= floor_soc + 0.5 and morning.desired_soc_pct <= floor_soc + 0.5:
                    return "NIGHT", None, morning.deadline, "soc_floor_reached"
                return "NIGHT", morning.export_w, morning.deadline, morning.reason

        # If Open-Meteo is unavailable, conservative/save strategies fail closed on
        # grid export. This is intentionally autumn-like: missing forecast must not
        # empty the battery based on yesterday's assumptions.
        if policy.tag in {"conservative", "save"}:
            return "DEGRADED", 0, None, "forecast_unavailable_preserve_battery"
        if policy.tag == "max-export":
            return "DEGRADED", day_export, None, "forecast_unavailable_max_export"
        return "DEGRADED", None, None, "forecast_unavailable"

    def correction_allowed(self, now: dt.datetime, soc: float, target_time: Optional[dt.datetime]) -> bool:
        if not target_time:
            return False
        plan = self.db.get("night_plan")
        if not plan or plan.get("cycle") != target_time.date().isoformat():
            return True
        desired_target_soc = float(self.db.get("current_night_target_soc", self.cfg.raw["battery"]["soc_floor_pct"]))
        stored_target_soc = float(plan.get("target_soc", self.cfg.raw["battery"]["soc_floor_pct"]))
        current_strategy = active_strategy(self.cfg.raw).tag
        stored_strategy = str(plan.get("strategy", "legacy"))
        # Explicit tag switches are immediate re-plans. Within CONSERVATIVE mode,
        # worsening forecasts may only preserve more battery; an improving forecast
        # does not re-open overnight export and create extra writes.
        if stored_strategy != current_strategy:
            return True
        if current_strategy == "conservative":
            if desired_target_soc >= stored_target_soc + 1.0:
                return True
            if desired_target_soc <= stored_target_soc - 1.0:
                return False
        elif abs(desired_target_soc - stored_target_soc) >= 1.0:
            return True
        corrections = int(plan.get("corrections", 0))
        if corrections >= int(cget(self.cfg, "control", "max_night_corrections", 1)):
            return False
        try:
            start = dt.datetime.fromisoformat(plan["start"])
            start_soc = float(plan["start_soc"])
            t = dt.datetime.fromisoformat(plan["target_time"])
        except Exception:
            return True
        plan_target_soc = float(plan.get("target_soc", self.db.get("current_night_target_soc", self.cfg.raw["battery"]["soc_floor_pct"])))
        expected = expected_soc_on_linear_plan(
            start_soc,
            plan_target_soc,
            start,
            t,
            now,
        )
        error = soc - expected
        self.db.set("night_soc_error_pct", round(error, 2), now)
        return abs(error) >= float(cget(self.cfg, "control", "soc_correction_threshold_pct", 5.0))

    def mark_night_write(self, now: dt.datetime, soc: float, target_time: dt.datetime) -> None:
        plan = self.db.get("night_plan")
        cycle = target_time.date().isoformat()
        if not plan or plan.get("cycle") != cycle:
            plan = {
                "cycle": cycle,
                "start": now.isoformat(),
                "start_soc": soc,
                "target_time": target_time.isoformat(),
                "target_soc": float(self.db.get("current_night_target_soc", self.cfg.raw["battery"]["soc_floor_pct"])),
                "strategy": active_strategy(self.cfg.raw).tag,
                "corrections": 0,
            }
        else:
            plan["corrections"] = int(plan.get("corrections", 0)) + 1
            plan["target_time"] = target_time.isoformat()
            plan["target_soc"] = float(self.db.get("current_night_target_soc", self.cfg.raw["battery"]["soc_floor_pct"]))
            plan["strategy"] = active_strategy(self.cfg.raw).tag
        self.db.set("night_plan", plan, now)

    def writes_today(self, now: dt.datetime) -> int:
        """Confirmed successful setting changes today (status=666 only)."""
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.db.successful_writes_since(start.isoformat())

    def order_submissions_today(self, now: dt.datetime) -> int:
        """Positive-orderId submissions today, including confirmed failures."""
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.db.order_submissions_since(start.isoformat())

    def can_write(self, now: dt.datetime, target_w: int, reason: str) -> Tuple[bool, str]:
        hard = int(self.cfg.raw["grid"]["export_hard_limit_w"])
        if not (0 <= target_w <= hard):
            return False, f"target {target_w} outside hard limit 0..{hard}"
        if self.active_order is not None:
            return False, "accepted order is still pending"

        # Two independent budgets in v3:
        #   1) wear budget = confirmed successful status=666 changes only;
        #   2) anti-storm budget = all positive-orderId submissions, including failures.
        # A Deye status=500 failed order therefore no longer burns one of the 4 normal
        # setting-change slots, but repeated failures are still bounded.
        submissions = self.order_submissions_today(now)
        max_submissions = max(1, int(cget(self.cfg, "control", "max_order_submissions_per_day", 8)))
        if submissions >= max_submissions:
            return False, f"daily order-submission safety budget exhausted ({submissions}/{max_submissions})"

        successes = self.writes_today(now)
        normal_max = max(1, int(cget(self.cfg, "control", "max_successful_writes_per_day", cget(self.cfg, "control", "max_writes_per_day", 4))))
        bonus_enabled = bool(cget(self.cfg, "control", "bonus_write_enabled", True))
        bonus_max = max(normal_max, int(cget(self.cfg, "control", "max_successful_writes_with_bonus", 5)))
        bonus_delta = max(0, int(cget(self.cfg, "control", "bonus_write_delta_w", 500)))
        if successes >= normal_max:
            delta = abs(target_w - self.current_setting_w) if self.current_setting_w is not None else 0
            bonus_ok = bonus_enabled and successes < bonus_max and delta >= bonus_delta
            if not bonus_ok:
                return False, f"daily successful-write budget exhausted ({successes}/{normal_max}; bonus needs delta>={bonus_delta}W)"

        if reason == "day_energy_budget":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            max_day = max(0, int(cget(self.cfg, "day_strategy", "max_budget_writes_per_day", 1)))
            if self.db.successful_writes_since_by_reason(start.isoformat(), "day_energy_budget") >= max_day:
                return False, "day energy-budget successful-write allowance exhausted"
        block_until = self.db.get("failed_order_retry_after")
        if block_until:
            try:
                until = dt.datetime.fromisoformat(block_until)
                if now < until:
                    return False, f"failed-order backoff until {until.isoformat()}"
            except Exception:
                pass
        uncertain_until = self.db.get("uncertain_write_guard_until")
        if uncertain_until:
            try:
                until = dt.datetime.fromisoformat(uncertain_until)
                if now < until:
                    return False, f"uncertain-submit guard until {until.isoformat()}"
                self.db.delete("uncertain_write_guard_until")
            except Exception:
                pass
        # A confirmed FAILED order must not trigger the long successful-write
        # cooldown. Deye already told us it failed, so only the short
        # failed_order_retry_minutes backoff applies. The long cooldown is based on
        # the last confirmed successful setting change.
        last = self.db.last_successful_write()
        if last:
            try:
                last_ts = dt.datetime.fromisoformat(last["updated_at"] or last["ts"])
                age_min = (now - last_ts).total_seconds() / 60.0
                if reason != "morning_day_restore" and age_min < int(cget(self.cfg, "control", "min_write_interval_minutes", 120)):
                    return False, f"successful-write cooldown active ({age_min:.0f} min)"
            except Exception:
                pass
        if self.current_setting_w is not None:
            delta = abs(target_w - self.current_setting_w)
            if delta < int(cget(self.cfg, "control", "min_write_delta_w", 200)):
                return False, f"delta too small ({delta} W)"
        return True, "ok"

    def set_control_path_state(self, now: dt.datetime, state: str, detail: str = "") -> None:
        self.db.set("control_path_state", state, now)
        self.db.set("control_path_last_attempt_at", now.isoformat(), now)
        if detail:
            self.db.set("control_path_last_detail", detail[:1000], now)
        if state == "ONLINE":
            self.db.set("control_path_last_online_at", now.isoformat(), now)

    def mark_uncertain_submit(self, now: dt.datetime, detail: str) -> None:
        """Block automatic retries after an ambiguous write submission.

        A timeout/network/unknown response could have reached Deye even if this process
        never received an orderId. Retrying immediately could therefore duplicate a
        real inverter-setting write. Prefer a conservative guard over flash wear.
        """
        guard_min = max(30, int(cget(self.cfg, "control", "uncertain_submit_guard_minutes", 120)))
        until = now + dt.timedelta(minutes=guard_min)
        self.db.set("uncertain_write_guard_until", until.isoformat(), now)
        self.db.set("uncertain_write_detail", detail[:1000], now)
        self.set_control_path_state(now, "UNCERTAIN", detail)

    def submit_write(self, now: dt.datetime, target_w: int, reason: str, context: Dict[str, Any]) -> str:
        hard = int(self.cfg.raw["grid"]["export_hard_limit_w"])
        target_w = int(clamp(target_w, 0, hard))
        previous = self.current_setting_w
        allowed, why = self.can_write(now, target_w, reason)
        if not allowed:
            self.log.emit("INFO", "write_skipped", target_w=target_w, current_w=previous, reason=reason, why=why)
            return "blocked"

        if bool(cget(self.cfg, "control", "dry_run", True)):
            # v3.1.0 dry-run is purely local: it does NOT issue Dynamic Control READ
            # commands. This prevents commissioning observation from occupying the
            # Deye command queue.
            self.set_control_path_state(now, "DRY_RUN", "no control API call made")
            # Log at most once per target/reason in a 30-minute window.
            last = self.db.get("last_dry_run") or {}
            recent = False
            try:
                last_at = dt.datetime.fromisoformat(last.get("ts", ""))
                recent = (now - last_at).total_seconds() < 1800
            except Exception:
                pass
            if not (recent and last.get("target_w") == target_w and last.get("reason") == reason):
                self.db.add_write(now, previous, target_w, reason, None, "dry-run", why, accepted=False)
                self.db.set("last_dry_run", {"ts": now.isoformat(), "target_w": target_w, "reason": reason}, now)
                self.log.emit("INFO", "write_dry_run", previous_w=previous, target_w=target_w, reason=reason)
            return "dry-run"

        write_api = str(cget(self.cfg, "control", "write_api", "power_update"))
        sn = str(self.cfg.raw["deye"]["inverter_sn"])
        try:
            if write_api == "dynamic_control":
                response = self.client.dynamic_control(sn, max_sell_power=target_w)
            else:
                response = self.client.set_max_sell_power(sn, target_w)
        except Exception as exc:
            if is_offline_error(exc):
                self.set_control_path_state(now, "OFFLINE", str(exc))
                self.db.add_api_event(now, "write_attempt_offline", True, str(exc))
                self.log.emit("INFO", "write_not_accepted_offline", target_w=target_w, reason=reason, api=write_api)
                return "offline"
            if is_busy_error(exc):
                self.set_control_path_state(now, "BUSY", str(exc))
                self.db.add_api_event(now, "write_attempt_busy", True, str(exc))
                self.log.emit("INFO", "write_not_accepted_busy", target_w=target_w, reason=reason, api=write_api)
                return "busy"
            # Ambiguous failures (timeout/network/unknown server response) are not
            # safe to retry immediately: the request might have reached Deye while
            # the response was lost. Do not count it as accepted, but guard against
            # duplicate writes for a conservative interval.
            self.mark_uncertain_submit(now, str(exc))
            self.db.add_api_event(now, "write_submit_uncertain", False, str(exc))
            self.log.emit(
                "ERROR",
                "write_submit_uncertain",
                target_w=target_w,
                reason=reason,
                api=write_api,
                error=str(exc),
                guard_minutes=max(30, int(cget(self.cfg, "control", "uncertain_submit_guard_minutes", 120))),
            )
            return "uncertain"

        accepted, order_id, why_response = control_response_accepted(response)
        if not accepted or order_id is None:
            detail = json.dumps(response, ensure_ascii=False)
            if why_response == "device_offline":
                self.set_control_path_state(now, "OFFLINE", detail)
                self.db.add_api_event(now, "write_attempt_offline", True, detail)
                self.log.emit("INFO", "write_not_accepted_offline", target_w=target_w, reason=reason, api=write_api)
                return "offline"
            # A response without a positive orderId is ambiguous unless it is
            # explicitly OFFLINE. Avoid immediate retries for EEPROM/flash safety.
            self.mark_uncertain_submit(now, f"{why_response}: {detail}")
            self.db.add_api_event(now, "write_submit_uncertain", False, f"{why_response}: {detail}")
            self.log.emit(
                "WARN",
                "write_not_accepted_uncertain",
                target_w=target_w,
                reason=reason,
                api=write_api,
                response=response,
            )
            return "uncertain"

        detail = json.dumps(response, ensure_ascii=False)
        self.set_control_path_state(now, "ONLINE", f"accepted orderId={order_id} target={target_w}")
        self.db.delete("uncertain_write_guard_until")
        self.db.delete("uncertain_write_detail")
        self.db.add_write(now, previous, target_w, reason, order_id, "pending", detail, accepted=True)
        self.active_order = self.db.get_write_by_order_id(order_id)
        self.active_context = context
        self.db.set("active_order_context", context, now)
        self.db.set("active_order_id", order_id, now)
        self.next_order_poll_at = now + dt.timedelta(seconds=max(5, int(cget(self.cfg, "control", "order_status_poll_seconds", 15))))
        self.db.add_api_event(now, "write_submit", True, f"accepted orderId={order_id} target={target_w}")
        self.log.emit(
            "INFO",
            "write_accepted",
            previous_w=previous,
            target_w=target_w,
            reason=reason,
            api=write_api,
            order_id=order_id,
            connection_status=response.get("connectionStatus"),
        )
        return "accepted"

    def clear_active_order(self, now: dt.datetime) -> None:
        self.active_order = None
        self.active_context = {}
        self.db.delete("active_order_context")
        self.db.delete("active_order_id")
        self.next_order_poll_at = None

    def poll_active_order(self, now: dt.datetime) -> str:
        if self.active_order is None:
            return "none"
        if self.next_order_poll_at and now < self.next_order_poll_at:
            return "pending"
        order_id = as_int(self.active_order["order_id"])
        if not order_id:
            # Defensive: accepted rows should always have an orderId.
            self.log.emit("ERROR", "pending_order_missing_id")
            return "pending_invalid"
        self.next_order_poll_at = now + dt.timedelta(seconds=max(5, int(cget(self.cfg, "control", "order_status_poll_seconds", 15))))
        try:
            status = self.client.check_order_status(order_id)
        except Exception as exc:
            self.db.set("active_order_last_poll_error", str(exc)[:500], now)
            self.log.emit("WARN", "order_status_read_failed", order_id=order_id, error=str(exc))
            return "pending"
        code = status.get("status")
        detail = json.dumps(status, ensure_ascii=False)
        if code in (666, "666"):
            self.db.update_write_status(order_id, "success", detail, now)
            target = int(self.active_order["requested_w"])
            self.current_setting_w = target
            self.db.set("last_known_setting_w", target, now)
            self.db.add_api_event(now, "write_confirmed", True, f"orderId={order_id} target={target}")
            self.log.emit("INFO", "write_success", order_id=order_id, target_w=target)
            if self.active_context.get("phase") == "NIGHT" and self.active_context.get("target_time"):
                try:
                    target_time = dt.datetime.fromisoformat(self.active_context["target_time"])
                    start_soc = float(self.active_context.get("soc"))
                    self.mark_night_write(now, start_soc, target_time)
                except Exception:
                    pass
            self.clear_active_order(now)
            return "success"
        if code in (500, "500"):
            self.db.update_write_status(order_id, "failed", detail, now)
            # A device-level rejection (Deye error 540) is not a transient cloud hiccup:
            # retrying it on the normal cadence only burns the daily submission ceiling,
            # so those get their own longer cooldown.
            error_code = str(status.get("error") or "").strip()
            reject_codes = {
                c.strip()
                for c in str(cget(self.cfg, "control", "device_reject_error_codes", "540")).split(",")
                if c.strip()
            }
            if error_code and error_code in reject_codes:
                backoff = int(cget(self.cfg, "control", "device_reject_retry_minutes", 45))
            else:
                backoff = int(cget(self.cfg, "control", "failed_order_retry_minutes", 15))
            self.db.set("failed_order_retry_after", (now + dt.timedelta(minutes=backoff)).isoformat(), now)
            self.db.add_api_event(now, "write_confirmed", False, f"orderId={order_id} failed: {detail}")
            self.log.emit(
                "ERROR",
                "write_failed",
                order_id=order_id,
                details=detail[:500],
                error_code=error_code or None,
                retry_after_minutes=backoff,
            )
            self.clear_active_order(now)
            return "failed"
        self.db.update_write_status(order_id, "pending", detail, now)
        self.log.emit("INFO", "order_pending", order_id=order_id, status=code)
        return "pending"

    def telemetry_allows_night_control(self, confidence: str, age_min: Optional[float]) -> Tuple[bool, str]:
        max_age = float(cget(self.cfg, "control", "max_control_telemetry_age_minutes", 90))
        if age_min is None or age_min > max_age:
            return False, "telemetry_too_old"
        if confidence not in {"FRESH", "ESTIMATED"}:
            return False, f"soc_confidence_{confidence.lower()}"
        return True, "ok"

    def maybe_control(
        self,
        now: dt.datetime,
        target_w: int,
        phase: str,
        reason: str,
        soc: float,
        target_time: Optional[dt.datetime],
        confidence: str,
        age_min: Optional[float],
    ) -> str:
        if self.active_order is not None:
            return "order_pending"
        cloud_offline_min = float(cget(self.cfg, "telemetry_health", "cloud_offline_minutes", 20))
        if age_min is None or age_min > cloud_offline_min:
            return "blocked_cloud_offline"
        if phase == "NIGHT":
            ok, why = self.telemetry_allows_night_control(confidence, age_min)
            if not ok:
                return f"blocked_{why}"

        allowed, why = self.can_write(now, target_w, reason)
        if not allowed:
            return f"blocked_{why.replace(' ', '_')}"

        retry_seconds = max(30, int(cget(self.cfg, "control", "offline_retry_seconds", 60)))
        if self.next_control_attempt_at and now < self.next_control_attempt_at:
            return "waiting_control_retry"
        self.next_control_attempt_at = now + dt.timedelta(seconds=retry_seconds)

        context = {
            "phase": phase,
            "reason": reason,
            "soc": soc,
            "target_time": target_time.isoformat() if target_time else None,
            "target_w": target_w,
        }

        # v3.1.0 deliberately performs NO preliminary Dynamic Control read. The
        # actual narrow write endpoint is the online probe. Explicit OFFLINE/BUSY
        # rejections create no orderId and are retried later; a positive orderId
        # immediately freezes duplicate submissions and enters status polling.
        result = self.submit_write(now, target_w, reason, context)
        return f"direct_{result}"

    def loop_once(self) -> int:
        now = dt.datetime.now(self.tz)
        self.refresh_forecast(now)

        order_state = self.poll_active_order(now)

        snap = self.fetch_device(now)
        if snap is None:
            return int(cget(self.cfg, "control", "loop_seconds", 60))
        # Do not extrapolate with a cached BatteryPower sample across an accepted
        # control order. The power operating point may have changed after the sample.
        allow_extrapolation = True
        # Disable BatteryPower-based extrapolation only when the operating point may
        # actually have changed: a still-pending accepted order or a confirmed
        # successful write newer than this telemetry sample. A confirmed status=500
        # failed order no longer poisons extrapolation for the rest of the night.
        possible_change_times = []
        if self.active_order is not None:
            try:
                possible_change_times.append(dt.datetime.fromisoformat(self.active_order["ts"]))
            except Exception:
                pass
        last_success = self.db.last_successful_write()
        if last_success:
            try:
                possible_change_times.append(dt.datetime.fromisoformat(last_success["updated_at"] or last_success["ts"]))
            except Exception:
                pass
        if snap.collection_at and any(t > snap.collection_at for t in possible_change_times):
            allow_extrapolation = False
        control_soc, confidence, age_min = derive_control_soc(
            self.cfg, snap, now, allow_power_extrapolation=allow_extrapolation
        )
        fresh = self.persist_snapshot(now, snap, control_soc, confidence, age_min)
        telemetry_health = self.update_telemetry_health(now, snap, age_min, fresh)
        if control_soc is None or not 0 <= control_soc <= 100:
            self.log.emit("ERROR", "invalid_soc", raw=snap.raw_soc, confidence=confidence)
            return int(cget(self.cfg, "control", "loop_seconds", 60))

        phase, recommended, target_time, reason = self.classify_and_decide(now, control_soc, snap)
        self.db.set("phase", phase, now)
        if target_time:
            self.db.set("target_time", target_time.isoformat(), now)
        if recommended is not None:
            recommended = int(clamp(recommended, 0, int(self.cfg.raw["grid"]["export_hard_limit_w"])))
            self.db.set("recommended_w", recommended, now)

        action = "none"
        detail_reason = reason
        if self.active_order is not None:
            action = f"order_{order_state}"
        elif recommended is not None:
            delta = None if self.current_setting_w is None else abs(recommended - self.current_setting_w)
            material = self.current_setting_w is None or delta >= int(cget(self.cfg, "control", "min_write_delta_w", 200))
            should_schedule = material
            if phase == "NIGHT" and should_schedule:
                should_schedule = self.correction_allowed(now, control_soc, target_time)
                if not should_schedule:
                    detail_reason += ":tracking_within_threshold"
            if should_schedule:
                action = self.maybe_control(
                    now,
                    recommended,
                    phase,
                    reason,
                    control_soc,
                    target_time,
                    confidence,
                    age_min,
                )
            else:
                action = "no_material_change"

        decision_target_soc = None
        if phase == "NIGHT":
            decision_target_soc = float(self.db.get("current_night_target_soc", self.cfg.raw["battery"]["soc_floor_pct"]))
        elif phase == "DAY":
            decision_target_soc = self.db.get("current_day_target_soc")
        self.db.add_decision(
            now,
            phase,
            control_soc,
            decision_target_soc,
            target_time,
            recommended,
            self.current_setting_w,
            action,
            detail_reason,
        )
        self.log.emit(
            "INFO",
            "control_tick",
            phase=phase,
            soc=round(control_soc, 2),
            raw_soc=snap.raw_soc,
            soc_confidence=confidence,
            telemetry_age_min=round(age_min, 1) if age_min is not None else None,
            collection_at=snap.collection_at.isoformat() if snap.collection_at else None,
            new_sample=fresh,
            fresh=fresh,  # legacy name retained for log consumers
            battery_w=snap.battery_power_w,
            grid_w=snap.grid_power_w,
            load_w=snap.consumption_power_w,
            pv_w=snap.solar_power_w,
            recommended_w=recommended,
            current_setting_w=self.current_setting_w,
            target_time=target_time.isoformat() if target_time else None,
            successful_writes_today=self.writes_today(now),
            order_submissions_today=self.order_submissions_today(now),
            accepted_writes_today=self.writes_today(now),  # compatibility: now means confirmed successful changes
            active_order_id=self.active_order["order_id"] if self.active_order is not None else None,
            telemetry_health=telemetry_health,
            strategy=active_strategy(self.cfg.raw).tag,
            night_target_soc=self.db.get("current_night_target_soc"),
            day_target_soc=self.db.get("current_day_target_soc"),
            action=action,
        )

        if self.active_order is not None:
            return max(5, int(cget(self.cfg, "control", "order_status_poll_seconds", 15)))
        if recommended is not None and action in {"direct_offline", "direct_busy", "waiting_control_retry"}:
            return min(
                int(cget(self.cfg, "control", "loop_seconds", 60)),
                max(30, int(cget(self.cfg, "control", "offline_retry_seconds", 60))),
            )
        return int(cget(self.cfg, "control", "loop_seconds", 60))

    def run(self) -> None:
        self.log.emit(
            "INFO",
            "controller_start",
            pid=os.getpid(),
            version="3.1.5",
            dry_run=bool(cget(self.cfg, "control", "dry_run", True)),
            write_api=str(cget(self.cfg, "control", "write_api", "power_update")),
            control_mode="direct_write_only",
        )
        self.refresh_forecast(dt.datetime.now(self.tz), force=True)
        while not self.stop:
            sleep_s = self.loop_once()
            for _ in range(max(1, sleep_s)):
                if self.stop:
                    break
                time.sleep(1)
        self.log.emit("INFO", "controller_stop")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", "--config", dest="config", default=DEFAULT_ENV, help="v3 .env file (legacy --config alias accepted)")
    ap.add_argument("--once", action="store_true", help="Run one control iteration then exit")
    args = ap.parse_args()
    cfg = load_config(args.config)
    ctl = Controller(cfg)

    def stop_handler(signum: int, frame: Any) -> None:
        ctl.stop = True

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    try:
        if args.once:
            ctl.refresh_forecast(dt.datetime.now(ctl.tz), force=True)
            ctl.loop_once()
        else:
            ctl.run()
        return 0
    finally:
        ctl.close()


if __name__ == "__main__":
    raise SystemExit(main())
