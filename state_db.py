#!/usr/bin/env python3
"""SQLite state and audit log for Deye Solar Optimizer v3.1 analytics-ready state."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from typing import Any, Dict, Iterable, Optional


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_at TEXT NOT NULL,
  logger_at TEXT,
  soc REAL,
  generation_power REAL,
  consumption_power REAL,
  grid_power REAL,
  wire_power REAL,
  battery_power REAL,
  raw_json TEXT NOT NULL,
  source TEXT,
  device_state INTEGER,
  battery_voltage REAL,
  ups_power REAL,
  control_soc REAL,
  soc_confidence TEXT,
  telemetry_age_minutes REAL,
  daily_production_kwh REAL,
  daily_consumption_kwh REAL,
  total_buy_kwh REAL,
  total_sell_kwh REAL,
  total_charge_kwh REAL,
  total_discharge_kwh REAL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_logger_at ON telemetry(logger_at);
CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fetched_at TEXT NOT NULL,
  target_date TEXT NOT NULL,
  sunrise TEXT NOT NULL,
  sunset TEXT NOT NULL,
  pv_wakeup TEXT NOT NULL,
  useful_pv_start TEXT NOT NULL,
  expected_kwh REAL NOT NULL,
  array_kwh_json TEXT NOT NULL,
  points_json TEXT,
  lead_bucket TEXT
);
CREATE INDEX IF NOT EXISTS idx_forecast_date ON forecasts(target_date, fetched_at);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  phase TEXT NOT NULL,
  current_soc REAL,
  target_soc REAL,
  target_time TEXT,
  recommended_w INTEGER,
  current_setting_w INTEGER,
  action TEXT NOT NULL,
  reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS writes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  previous_w INTEGER,
  requested_w INTEGER NOT NULL,
  reason TEXT NOT NULL,
  order_id TEXT,
  status TEXT NOT NULL,
  details TEXT,
  accepted INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_writes_ts ON writes(ts);
CREATE INDEX IF NOT EXISTS idx_writes_order_id ON writes(order_id);
CREATE TABLE IF NOT EXISTS api_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  operation TEXT NOT NULL,
  ok INTEGER NOT NULL,
  message TEXT
);
"""


class StateDB:
    def __init__(self, path: str, readonly: bool = False):
        self.path = path
        self.readonly = bool(readonly)
        if self.readonly:
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
            self.conn.row_factory = sqlite3.Row
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.conn = sqlite3.connect(path, timeout=15)
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _columns(self, table: str) -> set[str]:
        return {str(r[1]) for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _add_column(self, table: str, column: str, declaration: str) -> None:
        if column not in self._columns(table):
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _migrate(self) -> None:
        telemetry_cols = {
            "source": "TEXT",
            "device_state": "INTEGER",
            "battery_voltage": "REAL",
            "ups_power": "REAL",
            "control_soc": "REAL",
            "soc_confidence": "TEXT",
            "telemetry_age_minutes": "REAL",
            "daily_production_kwh": "REAL",
            "daily_consumption_kwh": "REAL",
            "total_buy_kwh": "REAL",
            "total_sell_kwh": "REAL",
            "total_charge_kwh": "REAL",
            "total_discharge_kwh": "REAL",
        }
        for name, declaration in telemetry_cols.items():
            self._add_column("telemetry", name, declaration)

        self._add_column("forecasts", "points_json", "TEXT")
        self._add_column("forecasts", "lead_bucket", "TEXT")

        self._add_column("writes", "accepted", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("writes", "updated_at", "TEXT")
        # Backfill acceptance only where the older package received an orderId.
        self.conn.execute(
            "UPDATE writes SET accepted=1 WHERE accepted=0 AND order_id IS NOT NULL AND order_id!='' AND status!='dry-run'"
        )
        self.conn.execute(
            "UPDATE writes SET updated_at=ts WHERE updated_at IS NULL"
        )

    def close(self) -> None:
        self.conn.close()

    def set(self, key: str, value: Any, now: dt.datetime) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        self.conn.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, payload, now.isoformat()),
        )
        self.conn.commit()

    def delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE key=?", (key,))
        self.conn.commit()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def add_device_telemetry(
        self,
        observed_at: dt.datetime,
        collection_at: Optional[dt.datetime],
        values: Dict[str, Any],
        *,
        control_soc: Optional[float],
        soc_confidence: str,
        telemetry_age_minutes: Optional[float],
        raw_json: Dict[str, Any],
    ) -> None:
        self.conn.execute(
            """INSERT INTO telemetry(
                observed_at,logger_at,soc,generation_power,consumption_power,grid_power,wire_power,battery_power,raw_json,
                source,device_state,battery_voltage,ups_power,control_soc,soc_confidence,telemetry_age_minutes,
                daily_production_kwh,daily_consumption_kwh,total_buy_kwh,total_sell_kwh,total_charge_kwh,total_discharge_kwh
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observed_at.isoformat(),
                collection_at.isoformat() if collection_at else None,
                _num(values.get("SOC")),
                _num(values.get("TotalSolarPower")),
                _num(values.get("TotalConsumptionPower")),
                _num(values.get("TotalGridPower")),
                None,
                _num(values.get("BatteryPower")),
                json.dumps(raw_json, ensure_ascii=False),
                "device/latest",
                _int(values.get("__device_state")),
                _num(values.get("BatteryVoltage")),
                _num(values.get("UPSLoadPower")),
                control_soc,
                soc_confidence,
                telemetry_age_minutes,
                _num(values.get("DailyActiveProduction")),
                _num(values.get("DailyConsumption")),
                _num(values.get("TotalEnergyBuy")),
                _num(values.get("TotalEnergySell")),
                _num(values.get("TotalChargeEnergy")),
                _num(values.get("TotalDischargeEnergy")),
            ),
        )
        self.conn.commit()

    # Compatibility helper for old scripts/tests.
    def add_telemetry(self, observed_at: dt.datetime, logger_at: Optional[dt.datetime], data: Dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO telemetry(
                observed_at,logger_at,soc,generation_power,consumption_power,grid_power,wire_power,battery_power,raw_json,source
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                observed_at.isoformat(), logger_at.isoformat() if logger_at else None,
                _num(data.get("batterySOC")), _num(data.get("generationPower")),
                _num(data.get("consumptionPower")), _num(data.get("gridPower")),
                _num(data.get("wirePower")), _num(data.get("batteryPower")),
                json.dumps(data, ensure_ascii=False), "station/latest",
            ),
        )
        self.conn.commit()

    def add_forecast(self, f: Any) -> None:
        points = [
            {"time": p.time.isoformat(), "predicted_w": float(p.predicted_w)}
            for p in (getattr(f, "points", []) or [])
        ]
        bucket = forecast_lead_bucket(f.fetched_at, f.date)
        self.conn.execute(
            """INSERT INTO forecasts(
                fetched_at,target_date,sunrise,sunset,pv_wakeup,useful_pv_start,expected_kwh,array_kwh_json,points_json,lead_bucket
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                f.fetched_at.isoformat(), f.date.isoformat(), f.sunrise.isoformat(), f.sunset.isoformat(),
                f.pv_wakeup.isoformat(), f.useful_pv_start.isoformat(), float(f.expected_kwh),
                json.dumps(f.array_kwh, ensure_ascii=False), json.dumps(points, ensure_ascii=False), bucket,
            ),
        )
        self.conn.commit()

    def add_decision(self, now: dt.datetime, phase: str, soc: Optional[float], target_soc: Optional[float],
                     target_time: Optional[dt.datetime], recommended_w: Optional[int], current_setting_w: Optional[int],
                     action: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO decisions(ts,phase,current_soc,target_soc,target_time,recommended_w,current_setting_w,action,reason) VALUES(?,?,?,?,?,?,?,?,?)",
            (now.isoformat(), phase, soc, target_soc, target_time.isoformat() if target_time else None,
             recommended_w, current_setting_w, action, reason),
        )
        self.conn.commit()

    def add_write(self, now: dt.datetime, previous_w: Optional[int], requested_w: int, reason: str,
                  order_id: Optional[Any], status: str, details: str = "", accepted: bool = False) -> None:
        self.conn.execute(
            "INSERT INTO writes(ts,previous_w,requested_w,reason,order_id,status,details,accepted,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                now.isoformat(), previous_w, requested_w, reason,
                None if order_id is None else str(order_id), status, details[:4000],
                1 if accepted else 0, now.isoformat(),
            ),
        )
        self.conn.commit()

    def update_write_status(self, order_id: Any, status: str, details: str, now: dt.datetime) -> None:
        self.conn.execute(
            "UPDATE writes SET status=?,details=?,updated_at=? WHERE order_id=?",
            (status, details[:4000], now.isoformat(), str(order_id)),
        )
        self.conn.commit()

    def add_api_event(self, now: dt.datetime, operation: str, ok: bool, message: str = "") -> None:
        self.conn.execute(
            "INSERT INTO api_events(ts,operation,ok,message) VALUES(?,?,?,?)",
            (now.isoformat(), operation, 1 if ok else 0, message[:1500]),
        )
        self.conn.commit()

    def writes_since(self, start_iso: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) c FROM writes WHERE ts>=? AND accepted=1", (start_iso,)
        ).fetchone()["c"])


    def successful_writes_since(self, start_iso: str) -> int:
        """Confirmed status=666 changes since start; failed accepted orders do not consume wear budget."""
        return int(self.conn.execute(
            "SELECT COUNT(*) c FROM writes WHERE ts>=? AND accepted=1 AND status='success'",
            (start_iso,),
        ).fetchone()["c"])

    def order_submissions_since(self, start_iso: str) -> int:
        """Positive-orderId submissions, including later failures; separate anti-storm budget."""
        return int(self.conn.execute(
            "SELECT COUNT(*) c FROM writes WHERE ts>=? AND accepted=1", (start_iso,)
        ).fetchone()["c"])

    def successful_writes_since_by_reason(self, since_iso: str, reason: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM writes WHERE ts>=? AND accepted=1 AND status='success' AND reason=?",
            (since_iso, reason),
        ).fetchone()
        return int(row["c"])

    def last_successful_write(self) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM writes WHERE status='success' AND accepted=1 ORDER BY id DESC LIMIT 1").fetchone()

    def last_live_write(self) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM writes WHERE accepted=1 ORDER BY id DESC LIMIT 1").fetchone()

    def last_pending_write(self) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM writes WHERE status='pending' AND accepted=1 ORDER BY id DESC LIMIT 1").fetchone()

    def get_write_by_order_id(self, order_id: Any) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM writes WHERE order_id=? ORDER BY id DESC LIMIT 1", (str(order_id),)).fetchone()

    def accepted_writes_since_by_reason(self, since_iso: str, reason: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM writes WHERE ts>=? AND accepted=1 AND reason=?",
            (since_iso, reason),
        ).fetchone()
        return int(row["c"])

    def last_full_soc_at(self, threshold_pct: float = 99.5) -> Optional[dt.datetime]:
        row = self.conn.execute(
            "SELECT logger_at FROM telemetry WHERE soc>=? AND logger_at IS NOT NULL ORDER BY logger_at DESC LIMIT 1",
            (float(threshold_pct),),
        ).fetchone()
        if not row or not row["logger_at"]:
            return None
        try:
            return dt.datetime.fromisoformat(row["logger_at"])
        except Exception:
            return None

    def daily_energy_history(self, before_date: dt.date, days: int = 14) -> list[dict[str, Any]]:
        start = (before_date - dt.timedelta(days=max(1, int(days)))).isoformat()
        end = before_date.isoformat()
        rows = self.conn.execute(
            """
            SELECT substr(logger_at,1,10) AS day,
                   MAX(daily_consumption_kwh) AS consumption_kwh,
                   MAX(daily_production_kwh) AS production_kwh
            FROM telemetry
            WHERE logger_at IS NOT NULL
              AND substr(logger_at,1,10)>=?
              AND substr(logger_at,1,10)<?
            GROUP BY substr(logger_at,1,10)
            ORDER BY day DESC
            """,
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]

    def _completed_day_actuals(self, before_date: dt.date, days: int) -> dict[str, float]:
        return {
            str(r["day"]): float(r["production_kwh"])
            for r in self.daily_energy_history(before_date, days)
            if r.get("production_kwh") is not None and float(r["production_kwh"]) > 0
        }

    def forecast_accuracy_ratios(self, before_date: dt.date, days: int = 14) -> list[float]:
        """Actual daily PV divided by the earliest stored full-day forecast."""
        actuals = self._completed_day_actuals(before_date, days)
        ratios: list[float] = []
        for day, actual in actuals.items():
            fc = self.conn.execute(
                "SELECT expected_kwh FROM forecasts WHERE target_date=? ORDER BY fetched_at ASC LIMIT 1",
                (day,),
            ).fetchone()
            if not fc or fc["expected_kwh"] is None or float(fc["expected_kwh"]) <= 1.0:
                continue
            ratio = actual / float(fc["expected_kwh"])
            if 0.2 <= ratio <= 3.0:
                ratios.append(ratio)
        return ratios

    def forecast_accuracy_ratios_for_bucket(self, before_date: dt.date, days: int, bucket: str) -> list[float]:
        """Actual/forecast ratios using forecasts made at a comparable lead-time bucket."""
        actuals = self._completed_day_actuals(before_date, days)
        ratios: list[float] = []
        has_lead_bucket = "lead_bucket" in self._columns("forecasts")
        for day, actual in actuals.items():
            select_cols = "fetched_at,expected_kwh,lead_bucket" if has_lead_bucket else "fetched_at,expected_kwh"
            rows = self.conn.execute(
                f"SELECT {select_cols} FROM forecasts WHERE target_date=? ORDER BY fetched_at ASC",
                (day,),
            ).fetchall()
            candidates = []
            target = dt.date.fromisoformat(day)
            for r in rows:
                try:
                    fetched = dt.datetime.fromisoformat(r["fetched_at"])
                except Exception:
                    continue
                rb = (r["lead_bucket"] if has_lead_bucket else None) or forecast_lead_bucket(fetched, target)
                if rb == bucket and r["expected_kwh"] is not None and float(r["expected_kwh"]) > 1.0:
                    candidates.append(r)
            if not candidates:
                continue
            # Use the latest forecast in the same bucket: it is closest to the current information state.
            fc = candidates[-1]
            ratio = actual / float(fc["expected_kwh"])
            if 0.2 <= ratio <= 3.0:
                ratios.append(ratio)
        return ratios

    def morning_timing_errors(
        self, before_date: dt.date, days: int, threshold_w: float, sustained_minutes: int
    ) -> list[float]:
        """Actual minus forecast sustained-PV start, minutes, for completed well-observed days.

        Only forecasts containing v3.1 points_json can participate, so the learner remains
        dormant until enough post-upgrade history exists.
        """
        if "points_json" not in self._columns("forecasts"):
            return []
        start = before_date - dt.timedelta(days=max(1, int(days)))
        out: list[float] = []
        for offset in range((before_date - start).days):
            day = start + dt.timedelta(days=offset)
            day_s = day.isoformat()
            fc_rows = self.conn.execute(
                "SELECT * FROM forecasts WHERE target_date=? AND points_json IS NOT NULL ORDER BY fetched_at ASC",
                (day_s,),
            ).fetchall()
            if not fc_rows:
                continue
            # Prefer the latest day-ahead forecast; otherwise the earliest same-day forecast.
            chosen = None
            for r in fc_rows:
                try:
                    fetched = dt.datetime.fromisoformat(r["fetched_at"])
                except Exception:
                    continue
                if forecast_lead_bucket(fetched, day) == "day_ahead":
                    chosen = r
            if chosen is None:
                chosen = fc_rows[0]
            try:
                pts = json.loads(chosen["points_json"] or "[]")
                predicted = _first_sustained_json_points(pts, threshold_w, sustained_minutes)
            except Exception:
                predicted = None
            if predicted is None:
                continue
            rows = self.conn.execute(
                "SELECT logger_at,generation_power FROM telemetry WHERE logger_at IS NOT NULL AND substr(logger_at,1,10)=? ORDER BY logger_at",
                (day_s,),
            ).fetchall()
            actual = _first_sustained_telemetry(rows, threshold_w, sustained_minutes)
            if actual is None:
                continue
            diff = (actual - predicted).total_seconds() / 60.0
            if -180 <= diff <= 180:
                out.append(diff)
        return out

    def recent_api_failures(self, since_iso: str) -> int:
        return int(self.conn.execute("SELECT COUNT(*) c FROM api_events WHERE ts>=? AND ok=0", (since_iso,)).fetchone()["c"])

    def day_production_at(self, date: dt.date, at_or_before: Optional[dt.datetime] = None):
        """Inverter daily-production counter for a local date, with its sample time.

        Returns (kwh, logger_at) for the newest telemetry row on that date at or before
        the given time, or None. The counter is the inverter's own monotonic daily
        total, so no integration over patchy samples is needed.
        """
        sql = ("SELECT daily_production_kwh k, logger_at t FROM telemetry "
               "WHERE substr(logger_at,1,10)=? AND daily_production_kwh IS NOT NULL")
        args: list[Any] = [date.isoformat()]
        if at_or_before is not None:
            sql += " AND logger_at<=?"
            args.append(at_or_before.isoformat())
        sql += " ORDER BY logger_at DESC LIMIT 1"
        row = self.conn.execute(sql, tuple(args)).fetchone()
        if row is None or row["k"] is None or not row["t"]:
            return None
        try:
            return float(row["k"]), dt.datetime.fromisoformat(row["t"])
        except Exception:
            return None

    def latest_telemetry(self) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM telemetry ORDER BY id DESC LIMIT 1").fetchone()

    def latest_forecast(self) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM forecasts ORDER BY id DESC LIMIT 1").fetchone()

    def latest_decision(self) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 1").fetchone()

    def recent_writes(self, limit: int = 10) -> Iterable[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM writes ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()



def forecast_lead_bucket(fetched_at: dt.datetime, target_date: dt.date) -> str:
    local_date = fetched_at.date()
    if local_date < target_date:
        return "day_ahead"
    h = fetched_at.hour
    if h < 6:
        return "h00_06"
    if h < 9:
        return "h06_09"
    if h < 12:
        return "h09_12"
    if h < 15:
        return "h12_15"
    return "h15_24"


def _first_sustained_json_points(points: list[dict[str, Any]], threshold_w: float, sustained_minutes: int) -> Optional[dt.datetime]:
    clean = []
    for p in points:
        try:
            clean.append((dt.datetime.fromisoformat(str(p["time"])), float(p.get("predicted_w", 0.0))))
        except Exception:
            pass
    need = max(1, int((max(1, sustained_minutes) + 14) // 15))
    for i in range(len(clean)):
        window = clean[i:i+need]
        if len(window) == need and all(w >= threshold_w for _, w in window):
            return window[0][0]
    return None


def _first_sustained_telemetry(rows: Iterable[sqlite3.Row], threshold_w: float, sustained_minutes: int) -> Optional[dt.datetime]:
    clean = []
    seen = set()
    for r in rows:
        if not r["logger_at"] or r["generation_power"] is None or r["logger_at"] in seen:
            continue
        seen.add(r["logger_at"])
        try:
            clean.append((dt.datetime.fromisoformat(r["logger_at"]), float(r["generation_power"])))
        except Exception:
            pass
    for i, (t0, w0) in enumerate(clean):
        if w0 < threshold_w:
            continue
        end = t0 + dt.timedelta(minutes=max(1, sustained_minutes))
        ok = True
        last = t0
        for t, w in clean[i:]:
            if t > end:
                break
            if (t - last).total_seconds() > 15 * 60 or w < threshold_w:
                ok = False
                break
            last = t
        if ok and last >= end - dt.timedelta(minutes=7):
            return t0
    return None

def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
