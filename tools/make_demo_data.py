#!/usr/bin/env python3
"""Generate a synthetic state database so the dashboard can be demonstrated.

Everything here is invented. It models a plausible 10 kWp south-facing site with a
15 kWh battery behind a 1 kW export limit, and reproduces the situations the
optimizer exists to handle:

* a clear day where surplus PV exceeds what a 1 kW cap can carry;
* a curtailment episode - battery at its ceiling, PV still producing, export cap
  left below the limit, which is what the dashboard banner is for;
* a DeyeCloud stall, so the write-window timeline has a real blocked band;
* a device-rejected order (Deye error 540) alongside confirmed ones.

Usage:
    python3 tools/make_demo_data.py --out /tmp/demo/state.db
    python3 dashboard_server.py --env tools/demo.env --port 8788
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import sys
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from state_db import StateDB  # noqa: E402

TZ = ZoneInfo("Europe/Amsterdam")
CAP_W = 1000
BATT_KWH = 15.0
FLOOR = 15.0
HOUSE_W = 260.0
OVERHEAD_W = 130.0
PEAK_KW = 8.2
RNG = random.Random(20260907)


def bell(day: dt.date, at: dt.datetime, cloudiness: float) -> float:
    """Unit-less clear-sky shape with slow cloud structure; scaled later to a target kWh."""
    sunrise, sunset = 6.6, 20.1
    h = at.hour + at.minute / 60.0
    if not (sunrise < h < sunset):
        return 0.0
    frac = (h - sunrise) / (sunset - sunrise)
    clear = math.sin(math.pi * frac) ** 2.2
    wobble = 1.0 - cloudiness * (0.55 + 0.45 * math.sin(frac * 9.0 + day.toordinal()))
    return max(0.0, clear * wobble * (0.95 + 0.10 * RNG.random()))


def day_profile(day: dt.date, cloudiness: float, target_kwh: float):
    """15-minute potential-PV series scaled so the day integrates to target_kwh."""
    t = dt.datetime.combine(day, dt.time.min, tzinfo=TZ)
    shape = []
    for _ in range(96):
        shape.append((t, bell(day, t, cloudiness)))
        t += dt.timedelta(minutes=15)
    total = sum(v for _, v in shape) * 0.25 / 1000.0
    scale = (target_kwh / total) if total > 0 else 0.0
    return [(ts, v * scale) for ts, v in shape]


def forecast_for(day: dt.date, fetched_at: dt.datetime, target_kwh: float):
    pts = [SimpleNamespace(time=ts, predicted_w=w)
           for ts, w in day_profile(day, 0.0, target_kwh)]
    kwh = sum(p.predicted_w for p in pts) / 1000.0 * 0.25
    return SimpleNamespace(
        date=day,
        sunrise=dt.datetime.combine(day, dt.time(6, 36), tzinfo=TZ),
        sunset=dt.datetime.combine(day, dt.time(20, 6), tzinfo=TZ),
        pv_wakeup=dt.datetime.combine(day, dt.time(7, 15), tzinfo=TZ),
        useful_pv_start=dt.datetime.combine(day, dt.time(7, 45), tzinfo=TZ),
        expected_kwh=kwh,
        array_kwh={"south_roof": kwh},
        points=pts,
        fetched_at=fetched_at,
    )


# Per-day scenario: (cloudiness, actual PV kWh, forecast PV kWh, export cap held)
# The final day is the showcase: a clear day where the cap was left low, the battery
# fills, and surplus PV is curtailed - which is what the dashboard is built to catch.
SCENARIOS = [
    (0.55, 17.0, 19.0, 1000),
    (0.18, 31.0, 32.0, 1000),
    (0.42, 22.0, 27.0, 1000),
    (0.12, 34.0, 35.0, 1000),
    (0.06, 36.0, 37.0, 400),
]


def build(out: Path, days: int = 5) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    db = StateDB(str(out))

    today = dt.datetime.now(TZ).date()
    start = today - dt.timedelta(days=days - 1)
    now = dt.datetime.now(TZ)

    # Cumulative inverter meters; the analytics read day deltas from these.
    sell = buy = charged = discharged = 0.0
    soc = 62.0
    order_id = 903_100_000_000_000

    for d in range(days):
        day = start + dt.timedelta(days=d)
        cloudiness, pv_kwh, fc_kwh, cap_w = SCENARIOS[d % len(SCENARIOS)]
        last_day = (d == days - 1)
        setting_w = cap_w

        db.add_forecast(forecast_for(day, dt.datetime.combine(day, dt.time(0, 5), tzinfo=TZ), fc_kwh * 1.10))
        db.add_forecast(forecast_for(day, dt.datetime.combine(day, dt.time(11, 20), tzinfo=TZ), fc_kwh))

        prod = cons = 0.0
        stall = (d == days - 3)   # a DeyeCloud stall, for the blocked band
        for ts, potential in day_profile(day, cloudiness, pv_kwh):
            if ts >= now:
                break
            if stall and dt.time(12, 40) <= ts.time() <= dt.time(15, 20):
                continue

            house = HOUSE_W + (1900.0 if ts.time().hour == 9 else 0.0)
            load = house + OVERHEAD_W

            # How much the battery could absorb this interval.
            headroom_kwh = max(0.0, (100.0 - soc) / 100.0 * BATT_KWH)
            max_charge_w = min(6000.0, headroom_kwh / 0.25 * 1000.0)

            # Export first up to the cap, battery takes the remainder; anything the
            # site cannot absorb is curtailed, so recorded PV falls below potential.
            export = min(float(setting_w), max(0.0, potential - load))
            charge = min(max(0.0, potential - load - export), max_charge_w)
            pv = min(potential, load + export + charge)
            curtailed = potential - pv

            if pv < load:                      # battery covers the shortfall
                deficit = load - pv
                usable_kwh = max(0.0, (soc - FLOOR) / 100.0 * BATT_KWH)
                discharge = min(deficit + float(setting_w), usable_kwh / 0.25 * 1000.0)
                export = max(0.0, min(float(setting_w), discharge - deficit))
                charge = 0.0
                batt_w = discharge
                imp = max(0.0, deficit - discharge)
            else:
                batt_w = -charge
                discharge = 0.0
                imp = 0.0

            soc = max(FLOOR, min(100.0, soc + (charge - discharge) * 0.25 / 1000.0 / BATT_KWH * 100.0))
            prod += pv * 0.25 / 1000.0
            cons += load * 0.25 / 1000.0
            sell += export * 0.25 / 1000.0
            buy += imp * 0.25 / 1000.0
            charged += charge * 0.25 / 1000.0
            discharged += discharge * 0.25 / 1000.0

            db.add_device_telemetry(
                ts, ts,
                {"SOC": f"{soc:.0f}", "TotalSolarPower": f"{pv:.0f}",
                 "BatteryPower": f"{batt_w:.0f}", "TotalGridPower": f"{imp - export:.0f}",
                 "TotalConsumptionPower": f"{load:.0f}", "UPSLoadPower": f"{house:.0f}",
                 "BatteryVoltage": "53.4", "DailyActiveProduction": f"{prod:.1f}",
                 "DailyConsumption": f"{cons:.1f}",
                 "TotalEnergySell": f"{sell:.1f}", "TotalEnergyBuy": f"{buy:.1f}",
                 "TotalChargeEnergy": f"{charged:.1f}", "TotalDischargeEnergy": f"{discharged:.1f}",
                 "__device_state": "1"},
                control_soc=soc, soc_confidence="FRESH", telemetry_age_minutes=1.2,
                raw_json={"DCPowerPV1": f"{pv*0.58:.0f}", "DCPowerPV2": f"{pv*0.42:.0f}"},
            )

            curtailing = soc >= 98.0 and curtailed > 100.0 and setting_w < CAP_W
            if stall and dt.time(15, 20) < ts.time() < dt.time(16, 40):
                action, rec = "blocked_cloud_offline", CAP_W
            elif curtailing:
                action, rec = "blocked_successful-write_cooldown_active_(96_min)", CAP_W
            else:
                rec = CAP_W if soc > 55 else (0 if soc < 30 else 400)
                action = "no_material_change"
            db.add_decision(ts, "DAY" if 7 <= ts.hour < 20 else "NIGHT",
                            soc, 96.0, None, rec, setting_w, action, "day_energy_budget")

        # Confirmed setting changes, plus a device rejection on the showcase day.
        plan = [(7, 35, 600, "morning_day_restore"), (9, 40, cap_w, "day_energy_budget"),
                (20, 25, 800, "forecast_protected_morning_reserve")]
        if last_day:
            plan = [(7, 35, 600, "morning_day_restore"),
                    (8, 55, 400, "day_energy_budget"),          # cap throttled down
                    (13, 20, CAP_W, "day_energy_budget")]       # reopen attempt, rejected
        prev_w = setting_w
        for hh, mm, target, reason in plan:
            when = dt.datetime.combine(day, dt.time(hh, mm), tzinfo=TZ)
            if when >= now:
                continue
            order_id += 7
            rejected = last_day and hh == 13
            db.add_write(when, prev_w, target, reason, order_id, "accepted", "{}", accepted=True)
            if rejected:
                db.update_write_status(order_id, "failed",
                                       json.dumps({"status": 500, "error": "540"}),
                                       when + dt.timedelta(seconds=68))
            else:
                db.update_write_status(order_id, "success", json.dumps({"status": 666}),
                                       when + dt.timedelta(seconds=16))
                prev_w = target

    # A representative day plan, so the dashboard's decision chain has content.
    # Built from the real dataclass so the field names cannot drift from the code.
    from energy_strategy import DayEnergyPlan
    plan = DayEnergyPlan(
        strategy_tag="conservative",
        planning_start_at=now.isoformat(),
        planning_soc_pct=100.0,
        morning_desired_soc_pct=None,
        morning_projected_soc_pct=None,
        target_soc_pct=96.0,
        target_reason="normal_daily_target",
        maintenance_due=False,
        last_full_at=now.isoformat(),
        raw_remaining_pv_kwh=9.42,
        base_safe_forecast_factor=0.80,
        safe_forecast_factor=0.70,
        intraday_bias=1.06,
        intraday_bias_source="intraday_window3h_r1.06_w1.00",
        bias_corrected_pv_kwh=9.99,
        safe_remaining_pv_kwh=6.99,
        forecast_factor_source="default_history_5d+strategy_conservative",
        forecast_distribution={},
        forecast_distribution_source="probabilistic_wait_h12_15_5/10",
        house_load_w=260.0,
        house_load_source="median_5d",
        house_energy_kwh=1.43,
        system_overhead_kwh=0.72,
        water_heater_kwh=0.0,
        water_heater_status="completed_or_past",
        cooker_kwh=0.0,
        cooker_status="disabled",
        scheduled_loads_kwh=0.0,
        battery_stored_kwh_needed=0.0,
        battery_input_kwh_needed=0.0,
        reserve_kwh=2.53,
        reserve_source="load_scaled_6.5h_at_390W_median_5d",
        end_of_day_target_soc_pct=63.7,
        end_of_day_target_reason="release_stored_surplus_forecast_refills",
        night_energy_need_kwh=4.78,
        night_hours=11.6,
        pv_surplus_kwh=4.84,
        stored_surplus_kwh=5.18,
        export_energy_budget_kwh=10.02,
        hours_to_sunset=5.6,
        cap_sustain_hours=10.02,
        full_export_margin_kwh=4.42,
        recommended_export_w=CAP_W,
        allocation_mode="cap_sustained",
    )
    db.set("day_energy_plan", plan.as_dict(), now)
    db.set("current_day_target_soc", 96.0, now)

    db.set("last_known_setting_w", SCENARIOS[(days - 1) % len(SCENARIOS)][3], now)
    db.set("recommended_w", CAP_W, now)
    db.set("phase", "DAY", now)
    db.set("active_strategy", "conservative", now)
    db.set("telemetry_health_state", "FRESH", now)
    db.close()
    print(f"demo database written: {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="/tmp/deye-demo/state.db")
    ap.add_argument("--days", type=int, default=5)
    args = ap.parse_args()
    build(Path(args.out), args.days)
