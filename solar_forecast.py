#!/usr/bin/env python3
"""Open-Meteo based multi-orientation PV forecast."""
from __future__ import annotations

import datetime as dt
import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List
from zoneinfo import ZoneInfo


class ForecastError(RuntimeError):
    pass


@dataclass
class PVArray:
    name: str
    kwp: float
    tilt_deg: float
    azimuth_deg: float


@dataclass
class ForecastPoint:
    time: dt.datetime
    predicted_w: float


@dataclass
class DayForecast:
    date: dt.date
    sunrise: dt.datetime
    sunset: dt.datetime
    pv_wakeup: dt.datetime
    useful_pv_start: dt.datetime
    expected_kwh: float
    array_kwh: Dict[str, float]
    points: List[ForecastPoint]
    fetched_at: dt.datetime


def _iso_local(value: str, tz: ZoneInfo) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _fetch_json(url: str, timeout: int = 15) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "deye-solar-optimizer-v2/2.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        raise ForecastError(f"Open-Meteo request failed: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ForecastError("Open-Meteo returned invalid JSON") from exc
    if data.get("error"):
        raise ForecastError(str(data.get("reason") or data))
    return data


def _request_array(
    latitude: float,
    longitude: float,
    timezone: str,
    array: PVArray,
    forecast_days: int = 3,
) -> Dict[str, Any]:
    params = {
        "latitude": f"{latitude:.6f}",
        "longitude": f"{longitude:.6f}",
        "minutely_15": "global_tilted_irradiance",
        "daily": "sunrise,sunset",
        "timezone": timezone,
        "forecast_days": str(forecast_days),
        "tilt": f"{array.tilt_deg:.2f}",
        "azimuth": f"{array.azimuth_deg:.2f}",
    }
    return _fetch_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params))


def fetch_forecast(
    latitude: float,
    longitude: float,
    timezone: str,
    arrays: Iterable[PVArray],
    performance_ratio: float,
    wake_threshold_w: float,
    useful_threshold_w: float,
    wakeup_bias_minutes: int = 0,
    forecast_days: int = 3,
) -> Dict[dt.date, DayForecast]:
    tz = ZoneInfo(timezone)
    arrays = list(arrays)
    if not arrays:
        raise ForecastError("At least one PV array is required")

    raw_by_array: Dict[str, Dict[str, Any]] = {}
    for array in arrays:
        raw_by_array[array.name] = _request_array(latitude, longitude, timezone, array, forecast_days)

    # Use the first response as the common time/sunrise/sunset axis.
    first = raw_by_array[arrays[0].name]
    times = first.get("minutely_15", {}).get("time") or []
    if not times:
        raise ForecastError("Open-Meteo response contains no minutely_15 time axis")

    per_array_values: Dict[str, List[float]] = {}
    for array in arrays:
        vals = raw_by_array[array.name].get("minutely_15", {}).get("global_tilted_irradiance") or []
        if len(vals) != len(times):
            raise ForecastError(f"Forecast axis mismatch for PV array {array.name}")
        per_array_values[array.name] = [float(v or 0.0) for v in vals]

    daily = first.get("daily", {})
    dates = daily.get("time") or []
    sunrises = daily.get("sunrise") or []
    sunsets = daily.get("sunset") or []
    if not (len(dates) == len(sunrises) == len(sunsets)):
        raise ForecastError("Incomplete daily sunrise/sunset data")

    # Build all 15-minute points. GTI is a preceding-15-minute mean, which is ideal
    # for energy integration. Predicted AC power is GTI/1000 * kWp * PR.
    all_points: List[ForecastPoint] = []
    all_components: Dict[str, List[float]] = {a.name: [] for a in arrays}
    for i, t in enumerate(times):
        timestamp = _iso_local(t, tz)
        total_w = 0.0
        for array in arrays:
            w = max(0.0, per_array_values[array.name][i]) * array.kwp * performance_ratio
            all_components[array.name].append(w)
            total_w += w
        all_points.append(ForecastPoint(timestamp, total_w))

    fetched_at = dt.datetime.now(tz)
    out: Dict[dt.date, DayForecast] = {}
    for d_text, sr_text, ss_text in zip(dates, sunrises, sunsets):
        day = dt.date.fromisoformat(d_text)
        sunrise = _iso_local(sr_text, tz)
        sunset = _iso_local(ss_text, tz)
        indices = [i for i, p in enumerate(all_points) if p.time.date() == day]
        points = [all_points[i] for i in indices]
        if not points:
            continue

        def first_crossing(threshold: float) -> dt.datetime:
            for p in points:
                if p.time >= sunrise and p.predicted_w >= threshold:
                    return p.time + dt.timedelta(minutes=wakeup_bias_minutes)
            return sunrise + dt.timedelta(minutes=wakeup_bias_minutes)

        wake = first_crossing(wake_threshold_w)
        useful = first_crossing(useful_threshold_w)

        # 15-minute means -> kWh = sum(kW * 0.25h)
        expected_kwh = sum(p.predicted_w for p in points) / 1000.0 * 0.25
        array_kwh: Dict[str, float] = {}
        for array in arrays:
            array_kwh[array.name] = sum(all_components[array.name][i] for i in indices) / 1000.0 * 0.25

        out[day] = DayForecast(
            date=day,
            sunrise=sunrise,
            sunset=sunset,
            pv_wakeup=wake,
            useful_pv_start=useful,
            expected_kwh=expected_kwh,
            array_kwh=array_kwh,
            points=points,
            fetched_at=fetched_at,
        )
    return out
