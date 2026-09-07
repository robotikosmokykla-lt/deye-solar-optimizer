#!/usr/bin/env python3
"""Named operating strategies for Deye Solar Optimizer v3.1.0.

The strategy tag is deliberately local configuration.  Switching a tag does not call
Deye directly; the normal controller remains the only component allowed to issue the
proven MAX_SELL_POWER write, with all existing cooldown/order/cloud-freshness guards.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class StrategyPolicy:
    tag: str
    description: str
    day_mode: str                 # budget | full_export | save | economic
    night_mode: str               # forecast_protected | floor | save | economic
    safe_factor_cap: float | None
    safe_factor_floor: float | None
    reserve_kwh: float            # fixed fallback, and the pre-v3.1.3 behaviour
    reserve_hours: float = 0.0    # reserve expressed as hours of measured house draw


PRESETS: Dict[str, StrategyPolicy] = {
    "conservative": StrategyPolicy(
        tag="conservative",
        description=(
            "Autumn/weak-solar default: preserve overnight battery when the next safe "
            "PV budget cannot refill it; conservative daytime export budget."
        ),
        day_mode="budget",
        night_mode="forecast_protected",
        safe_factor_cap=0.70,
        safe_factor_floor=None,
        reserve_kwh=2.50,
        reserve_hours=6.5,
    ),
    "risky": StrategyPolicy(
        tag="risky",
        description=(
            "Aggressive forecast-led export: drain toward the normal 15% morning floor "
            "and use an optimistic daytime PV allowance with only a small reserve."
        ),
        day_mode="budget",
        night_mode="floor",
        safe_factor_cap=None,
        safe_factor_floor=0.90,
        reserve_kwh=0.50,
        reserve_hours=1.3,
    ),
    "max-export": StrategyPolicy(
        tag="max-export",
        description=(
            "Maximum export: retain the configured sell cap whenever control is available and "
            "drain overnight toward 15% regardless of the following day's PV budget."
        ),
        day_mode="full_export",
        night_mode="floor",
        safe_factor_cap=None,
        safe_factor_floor=1.00,
        reserve_kwh=0.0,
        reserve_hours=0.0,
    ),
    "save": StrategyPolicy(
        tag="save",
        description=(
            "Maximum energy retention/self-use: request 0W grid export day and night; "
            "no intentional battery-to-grid discharge."
        ),
        day_mode="save",
        night_mode="save",
        safe_factor_cap=0.65,
        safe_factor_floor=None,
        reserve_kwh=3.00,
        reserve_hours=8.0,
    ),
    "economic": StrategyPolicy(
        tag="economic",
        description=(
            "Price-aware strategy: export genuine safe surplus, and only cycle battery "
            "for grid export when configured export revenue exceeds import opportunity cost plus wear."
        ),
        day_mode="economic",
        night_mode="economic",
        safe_factor_cap=0.80,
        safe_factor_floor=None,
        reserve_kwh=1.50,
        reserve_hours=4.0,
    ),
}

ALIASES = {
    "conservative": "conservative",
    "cons": "conservative",
    "safe": "conservative",
    "risky": "risky",
    "risk": "risky",
    "max-export": "max-export",
    "max_export": "max-export",
    "max export": "max-export",
    "max": "max-export",
    "export": "max-export",
    "save": "save",
    "saving": "save",
    "self-use": "save",
    "self_use": "save",
    "economic": "economic",
    "eco": "economic",
    "price": "economic",
}


def normalize_strategy_tag(value: Any) -> str:
    text = str(value or "conservative").strip().lower()
    text = " ".join(text.split())
    tag = ALIASES.get(text, text.replace("_", "-"))
    if tag not in PRESETS:
        raise ValueError(f"unknown strategy tag {value!r}; choose: {', '.join(PRESETS)}")
    return tag


def active_strategy(raw: Dict[str, Any]) -> StrategyPolicy:
    tag = normalize_strategy_tag(raw.get("strategy", {}).get("active", "conservative"))
    return PRESETS[tag]


def effective_safe_factor(base_factor: float, policy: StrategyPolicy) -> float:
    value = max(0.0, min(1.10, float(base_factor)))
    if policy.safe_factor_cap is not None:
        value = min(value, float(policy.safe_factor_cap))
    if policy.safe_factor_floor is not None:
        value = max(value, float(policy.safe_factor_floor))
    return max(0.0, min(1.05, value))
