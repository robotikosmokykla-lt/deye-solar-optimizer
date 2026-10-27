"""Calendar defaults only. Hardware changes belong to season_manager.py."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


def seasonal_defaults(values: dict[str, str], *, now: dt.datetime | None = None):
    mode = values.get("SEASON_MODE", "off").strip().lower() or "off"
    if mode not in {"off", "auto", "winter", "summer"}:
        raise ValueError("SEASON_MODE must be off, auto, winter or summer")
    months = [int(x.strip()) for x in values.get("SEASON_WINTER_MONTHS", "10,11,12,1,2,3").split(",")]
    if not months or any(x < 1 or x > 12 for x in months):
        raise ValueError("SEASON_WINTER_MONTHS must contain months 1..12")
    tz = ZoneInfo(values.get("SITE_TIMEZONE", "Etc/UTC"))
    local = dt.datetime.now(tz) if now is None else now.astimezone(tz)
    active = ("winter" if local.month in months else "summer") if mode == "auto" else mode
    ampere = int(values.get("SEASON_GRID_CHARGE_AMPERE", "20"))
    if not 1 <= ampere <= 200:
        raise ValueError("SEASON_GRID_CHARGE_AMPERE must be 1..200")
    defaults = {}
    if active != "off":
        reserve = values.get(f"SEASON_{active.upper()}_MORNING_SOC_PCT", "20" if active == "winter" else "18")
        defaults = {"NIGHT_MORNING_SOC_PCT": reserve, "NIGHT_MAX_EXPORT_W": "0"}
    return {"mode": mode, "active": active, "grid_charge_ampere": ampere}, defaults


def reserve_request(cfg: dict, current: dict) -> dict:
    """Preserve all TOU fields except SOC and the explicit reserve-charge flag.

    Fixed boundaries match the commissioned six-slot schedule. Refuse a custom
    schedule that spans a boundary rather than silently moving a user's times.
    """
    import copy

    if current.get("touAction") != "on":
        raise ValueError("Time of Use is disabled; enable it explicitly before applying seasonal reserves")
    items = copy.deepcopy(current.get("timeUseSettingItems", []))
    times = [x.get("time") for x in items]
    if len(items) != 6 or times != sorted(set(times)) or "07:00" not in times or "17:00" not in times:
        raise ValueError("Seasonal reserves require six ordered slots with 07:00 and 17:00 boundaries")
    day_soc = float(cfg["battery"]["soc_floor_pct"])
    night_soc = float(cfg["night_strategy"]["morning_soc_pct"])
    if day_soc != int(day_soc) or night_soc != int(night_soc):
        raise ValueError("Native reserve SOC values must be whole percentages")
    for item in items:
        item["soc"] = int(day_soc if "07:00" <= item["time"] < "17:00" else night_soc)
        item["enableGridCharge"] = True
    return {
        "gridChargeAction": "on",
        "gridChargeAmpere": cfg["season"]["grid_charge_ampere"],
        "timeUseSettingItems": items,
    }


def request_matches(current: dict, desired: dict) -> bool:
    return all(current.get(key) == value for key, value in desired.items())
