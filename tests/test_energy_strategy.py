import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from energy_strategy import (
    battery_target,
    build_day_energy_plan,
    build_morning_soc_plan,
    cooker_remaining_kwh,
    heater_remaining_kwh,
    night_floor_deadline,
    quantize_export_floor,
)
from state_db import StateDB


class EnergyStrategyTests(unittest.TestCase):
    def raw(self):
        return {
            "battery": {"effective_kwh": 15.0, "soc_floor_pct": 15.0, "day_target_soc_pct": 96.0},
            "maintenance": {"target_soc_pct": 100.0, "interval_days": 30, "full_soc_threshold_pct": 99.5},
            "grid": {"export_hard_limit_w": 1000, "day_export_w": 1000},
            "load_model": {"base_house_load_w": 115, "system_overhead_w": 130},
            "load_forecast": {
                "learning_days": 14,
                "min_learning_days": 3,
                "fallback_house_load_w": 230,
                "min_house_load_w": 100,
                "max_house_load_w": 600,
                "subtract_scheduled_heater": True,
            },
            "pv": {"wakeup_bias_minutes": 15},
            "night_strategy": {"morning_surplus_threshold_w": 350, "sustained_minutes": 30, "floor_lead_minutes": 10},
            "water_heater": {"enabled": True, "scheduled_time": "09:15", "scheduled_energy_kwh": 3.0, "duration_minutes": 90},
            "cooker": {"enabled": True, "scheduled_time": "18:00", "scheduled_energy_kwh": 1.0, "duration_minutes": 30},
            "forecast_uncertainty": {"default_safe_factor": 0.80, "learning_days": 14, "min_learning_days": 5, "lower_quantile": 0.2, "min_factor": 0.6, "max_factor": 1.05},
            "day_strategy": {"charge_efficiency": 0.91, "export_support_reserve_kwh": 1.5},
            "analytics": {"battery_discharge_efficiency": 0.95},
            "economic": {"import_eur_kwh": 0.25, "export_eur_kwh": 0.05, "battery_wear_eur_kwh": 0.02},
            "control": {"write_step_w": 100},
        }

    def test_quantize_export_floors_not_rounds_up(self):
        self.assertEqual(quantize_export_floor(999, 100, 1000), 900)
        self.assertEqual(quantize_export_floor(1100, 100, 1000), 1000)

    def test_night_floor_deadline(self):
        tz = ZoneInfo("Europe/Amsterdam")
        day = SimpleNamespace(
            sunrise=dt.datetime(2026, 9, 4, 6, 36, tzinfo=tz),
            pv_wakeup=dt.datetime(2026, 9, 4, 7, 0, tzinfo=tz),
            useful_pv_start=dt.datetime(2026, 9, 4, 7, 15, tzinfo=tz),
            points=[
                SimpleNamespace(time=dt.datetime(2026, 9, 4, 7, 0, tzinfo=tz), predicted_w=300),
                SimpleNamespace(time=dt.datetime(2026, 9, 4, 7, 15, tzinfo=tz), predicted_w=400),
                SimpleNamespace(time=dt.datetime(2026, 9, 4, 7, 30, tzinfo=tz), predicted_w=500),
            ],
        )
        # 07:15 +15 site bias -10 lead = 07:20.
        self.assertEqual(night_floor_deadline(self.raw(), day), dt.datetime(2026, 9, 4, 7, 20, tzinfo=tz))

    def test_heater_budget_before_and_after_schedule(self):
        tz = ZoneInfo("Europe/Amsterdam")
        raw = self.raw()
        before = dt.datetime(2026, 9, 4, 9, 0, tzinfo=tz)
        midway = dt.datetime(2026, 9, 4, 10, 0, tzinfo=tz)
        after = dt.datetime(2026, 9, 4, 11, 0, tzinfo=tz)
        self.assertAlmostEqual(heater_remaining_kwh(raw, before)[0], 3.0)
        self.assertAlmostEqual(heater_remaining_kwh(raw, midway)[0], 1.5, places=2)
        self.assertEqual(heater_remaining_kwh(raw, after)[0], 0.0)

    def test_cooker_is_budgeted_as_scheduled_load(self):
        tz = ZoneInfo("Europe/Amsterdam")
        raw = self.raw()
        before = dt.datetime(2026, 9, 4, 17, 30, tzinfo=tz)
        during = dt.datetime(2026, 9, 4, 18, 15, tzinfo=tz)
        after = dt.datetime(2026, 9, 4, 18, 45, tzinfo=tz)
        self.assertAlmostEqual(cooker_remaining_kwh(raw, before)[0], 1.0)
        self.assertAlmostEqual(cooker_remaining_kwh(raw, during)[0], 0.5, places=2)
        self.assertEqual(cooker_remaining_kwh(raw, after)[0], 0.0)

    def test_recent_full_means_normal_96_target(self):
        tz = ZoneInfo("Europe/Amsterdam")
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            now = dt.datetime(2026, 9, 4, 12, 0, tzinfo=tz)
            db.set("last_full_balance_at", (now - dt.timedelta(days=2)).isoformat(), now)
            target = battery_target(self.raw(), db, now)
            self.assertEqual(target.target_soc_pct, 96.0)
            self.assertFalse(target.maintenance_due)
            db.close()

    def test_old_full_means_100_maintenance_target(self):
        tz = ZoneInfo("Europe/Amsterdam")
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            now = dt.datetime(2026, 10, 10, 12, 0, tzinfo=tz)
            db.set("last_full_balance_at", (now - dt.timedelta(days=35)).isoformat(), now)
            target = battery_target(self.raw(), db, now)
            self.assertEqual(target.target_soc_pct, 100.0)
            self.assertTrue(target.maintenance_due)
            db.close()

    def test_maintenance_is_deferred_on_poor_forecast(self):
        tz = ZoneInfo("Europe/Amsterdam")
        now = dt.datetime(2026, 10, 10, 10, 0, tzinfo=tz)
        sunset = dt.datetime(2026, 10, 10, 18, 0, tzinfo=tz)
        points = [
            SimpleNamespace(time=now + dt.timedelta(minutes=15*i), predicted_w=500)
            for i in range(32)
        ]
        day = SimpleNamespace(sunset=sunset, points=points)
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            db.set("last_full_balance_at", (now - dt.timedelta(days=40)).isoformat(), now)
            plan = build_day_energy_plan(self.raw(), db, day, now, 80.0)
            self.assertTrue(plan.maintenance_due)
            self.assertEqual(plan.target_soc_pct, 96.0)
            self.assertEqual(plan.target_reason, "maintenance_deferred_forecast")
            db.close()

    def test_abundant_pv_supports_full_export(self):
        tz = ZoneInfo("Europe/Amsterdam")
        now = dt.datetime(2026, 9, 4, 10, 0, tzinfo=tz)
        sunset = dt.datetime(2026, 9, 4, 20, 0, tzinfo=tz)
        points = [
            SimpleNamespace(time=now + dt.timedelta(minutes=15*i), predicted_w=6000)
            for i in range(40)
        ]
        day = SimpleNamespace(sunset=sunset, points=points)
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            db.set("last_full_balance_at", (now - dt.timedelta(days=2)).isoformat(), now)
            plan = build_day_energy_plan(self.raw(), db, day, now, 70.0)
            self.assertEqual(plan.target_soc_pct, 96.0)
            self.assertEqual(plan.recommended_export_w, 1000)
            self.assertGreater(plan.full_export_margin_kwh, 0)
            db.close()

    def poor_day(self, tz):
        sunrise = dt.datetime(2026, 9, 4, 6, 36, tzinfo=tz)
        sunset = dt.datetime(2026, 9, 4, 20, 9, tzinfo=tz)
        points = []
        t = dt.datetime(2026, 9, 4, 6, 30, tzinfo=tz)
        while t <= sunset:
            # ~10kWh raw day with a modest morning ramp.
            if t < dt.datetime(2026, 9, 4, 8, 0, tzinfo=tz):
                w = max(0, (t.hour * 60 + t.minute - 390) * 7)
            else:
                w = 850
            points.append(SimpleNamespace(time=t, predicted_w=w))
            t += dt.timedelta(minutes=15)
        return SimpleNamespace(
            sunrise=sunrise, sunset=sunset, points=points,
            pv_wakeup=dt.datetime(2026, 9, 4, 7, 30, tzinfo=tz),
            useful_pv_start=dt.datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        )

    def test_conservative_poor_day_stops_night_grid_export(self):
        tz = ZoneInfo("Europe/Amsterdam")
        now = dt.datetime(2026, 9, 4, 0, 40, tzinfo=tz)
        raw = self.raw()
        raw["strategy"] = {"active": "conservative"}
        day = self.poor_day(tz)
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            db.set("last_full_balance_at", (now - dt.timedelta(days=1)).isoformat(), now)
            morning = build_morning_soc_plan(raw, db, day, now, 64.0)
            self.assertGreater(morning.desired_soc_pct, 60.0)
            self.assertEqual(morning.export_w, 0)
            self.assertLess(morning.projected_soc_pct, 64.0)
            self.assertGreater(morning.projected_soc_pct, 45.0)
            db.close()

    def test_risky_poor_day_still_targets_15pct_floor(self):
        tz = ZoneInfo("Europe/Amsterdam")
        now = dt.datetime(2026, 9, 4, 0, 40, tzinfo=tz)
        raw = self.raw()
        raw["strategy"] = {"active": "risky"}
        day = self.poor_day(tz)
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            morning = build_morning_soc_plan(raw, db, day, now, 64.0)
            self.assertEqual(morning.desired_soc_pct, 15.0)
            self.assertGreater(morning.export_w, 0)
            db.close()

    def test_max_export_tag_ignores_poor_day_budget(self):
        tz = ZoneInfo("Europe/Amsterdam")
        now = dt.datetime(2026, 9, 4, 9, 0, tzinfo=tz)
        raw = self.raw()
        raw["strategy"] = {"active": "max-export"}
        day = self.poor_day(tz)
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            plan = build_day_energy_plan(raw, db, day, now, 20.0)
            self.assertEqual(plan.recommended_export_w, 1000)
            db.close()

    def test_save_tag_closes_export_cap(self):
        tz = ZoneInfo("Europe/Amsterdam")
        now = dt.datetime(2026, 9, 4, 9, 0, tzinfo=tz)
        raw = self.raw()
        raw["strategy"] = {"active": "save"}
        day = self.poor_day(tz)
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            plan = build_day_energy_plan(raw, db, day, now, 80.0)
            self.assertEqual(plan.recommended_export_w, 0)
            db.close()

    def test_predawn_plan_starts_at_morning_handoff_not_midnight(self):
        tz = ZoneInfo("Europe/Amsterdam")
        now = dt.datetime(2026, 9, 4, 0, 40, tzinfo=tz)
        raw = self.raw()
        raw["strategy"] = {"active": "conservative"}
        day = self.poor_day(tz)
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            plan = build_day_energy_plan(raw, db, day, now, 64.0)
            start = dt.datetime.fromisoformat(plan.planning_start_at)
            self.assertGreater(start.hour, 6)
            self.assertLess(plan.hours_to_sunset, 14.0)
            self.assertIsNotNone(plan.morning_projected_soc_pct)
            db.close()


    def test_economic_strategy_exports_when_battery_export_is_profitable(self):
        raw = self.raw()
        raw["strategy"] = {"active": "economic"}
        raw["economic"] = {"import_eur_kwh": 0.10, "export_eur_kwh": 0.30, "battery_wear_eur_kwh": 0.01}
        tz = ZoneInfo("Europe/Amsterdam")
        now = dt.datetime(2026, 9, 4, 0, 0, tzinfo=tz)
        day = SimpleNamespace(
            date=now.date(), sunrise=dt.datetime(2026,9,4,6,30,tzinfo=tz),
            sunset=dt.datetime(2026,9,4,20,0,tzinfo=tz),
            useful_pv_start=dt.datetime(2026,9,4,8,0,tzinfo=tz),
            pv_wakeup=dt.datetime(2026,9,4,7,0,tzinfo=tz),
            points=[SimpleNamespace(time=dt.datetime(2026,9,4,8,0,tzinfo=tz),predicted_w=400),SimpleNamespace(time=dt.datetime(2026,9,4,8,15,tzinfo=tz),predicted_w=400)],
        )
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(str(Path(td)/"state.db"))
            plan=build_morning_soc_plan(raw,db,day,now,80)
            self.assertEqual(plan.reason,"economic_battery_export_profitable")
            self.assertEqual(plan.desired_soc_pct,15.0)
            self.assertGreater(plan.export_w,0)
            db.close()

    def test_economic_strategy_preserves_battery_when_export_is_cheap(self):
        raw = self.raw()
        raw["strategy"] = {"active": "economic"}
        tz = ZoneInfo("Europe/Amsterdam")
        now = dt.datetime(2026, 9, 4, 0, 0, tzinfo=tz)
        day = SimpleNamespace(
            date=now.date(), sunrise=dt.datetime(2026,9,4,6,30,tzinfo=tz),
            sunset=dt.datetime(2026,9,4,20,0,tzinfo=tz),
            useful_pv_start=dt.datetime(2026,9,4,8,0,tzinfo=tz),
            pv_wakeup=dt.datetime(2026,9,4,7,0,tzinfo=tz),
            points=[SimpleNamespace(time=dt.datetime(2026,9,4,8,0,tzinfo=tz),predicted_w=400),SimpleNamespace(time=dt.datetime(2026,9,4,8,15,tzinfo=tz),predicted_w=400)],
        )
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(str(Path(td)/"state.db"))
            plan=build_morning_soc_plan(raw,db,day,now,80)
            self.assertEqual(plan.reason,"economic_preserve_for_self_use")
            db.close()

if __name__ == "__main__":
    unittest.main()


class StoredSurplusExportTests(unittest.TestCase):
    """v3.1.1: the daytime plan must be able to sell stored surplus, not only PV surplus."""

    tz = ZoneInfo("Europe/Amsterdam")

    def raw(self):
        return EnergyStrategyTests.raw(self)

    def _day(self, now, sunset, pv_w):
        steps = int((sunset - now).total_seconds() // 900) + 1
        return SimpleNamespace(
            date=now.date(),
            sunrise=now.replace(hour=6, minute=38),
            sunset=sunset,
            points=[
                SimpleNamespace(time=now + dt.timedelta(minutes=15 * i), predicted_w=pv_w)
                for i in range(steps)
            ],
        )

    def _tomorrow(self, day_after, peak_w):
        sunrise = day_after.replace(hour=6, minute=40)
        sunset = day_after.replace(hour=20, minute=4)
        points = []
        t = day_after.replace(hour=0, minute=0)
        while t <= sunset:
            inside = sunrise <= t <= sunset
            points.append(SimpleNamespace(time=t, predicted_w=peak_w if inside else 0.0))
            t += dt.timedelta(minutes=15)
        return SimpleNamespace(
            date=day_after.date(),
            sunrise=sunrise,
            pv_wakeup=sunrise + dt.timedelta(minutes=30),
            useful_pv_start=sunrise + dt.timedelta(minutes=60),
            sunset=sunset,
            points=points,
        )

    def _plan(self, soc, today_pv_w, tomorrow_peak_w, raw=None):
        now = dt.datetime(2026, 9, 5, 12, 51, tzinfo=self.tz)
        sunset = dt.datetime(2026, 9, 5, 20, 6, tzinfo=self.tz)
        day = self._day(now, sunset, today_pv_w)
        tomorrow = self._tomorrow(dt.datetime(2026, 9, 6, 0, 0, tzinfo=self.tz), tomorrow_peak_w)
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            db.set("last_full_balance_at", (now - dt.timedelta(days=1)).isoformat(), now)
            try:
                return build_day_energy_plan(raw or self.raw(), db, day, now, soc, tomorrow)
            finally:
                db.close()

    def test_full_battery_with_strong_tomorrow_exports_at_the_cap(self):
        """Regression: full battery, weak remaining PV, strong forecast for tomorrow.

        Earlier versions recommended 0 W in this state, holding the export cap shut
        while the battery was full and surplus PV was being curtailed.
        """
        plan = self._plan(soc=99.0, today_pv_w=800, tomorrow_peak_w=4500)
        self.assertEqual(plan.end_of_day_target_reason, "release_stored_surplus_forecast_refills")
        self.assertLess(plan.end_of_day_target_soc_pct, 96.0)
        self.assertGreater(plan.stored_surplus_kwh, 3.0)
        # Earlier versions recommended 0 W for this exact state.
        self.assertGreaterEqual(plan.recommended_export_w, 700)

    def test_reserve_is_an_end_of_day_floor_not_an_export_deduction(self):
        plan = self._plan(soc=99.0, today_pv_w=800, tomorrow_peak_w=4500)
        floor = 15.0
        batt = 15.0
        expected = floor + (plan.night_energy_need_kwh + plan.reserve_kwh) / batt * 100.0
        self.assertAlmostEqual(plan.end_of_day_target_soc_pct, expected, places=6)
        # The reserve stays whole in the battery instead of being skimmed off export.
        self.assertGreater(plan.reserve_kwh, 0.0)

    def test_weak_tomorrow_still_holds_the_day_target(self):
        plan = self._plan(soc=99.0, today_pv_w=800, tomorrow_peak_w=150)
        self.assertEqual(plan.end_of_day_target_reason, "hold_day_target_forecast_cannot_refill")
        self.assertGreaterEqual(plan.end_of_day_target_soc_pct, 96.0)
        # Only the sliver above the 96% day target is surplus; the reserve stays put.
        self.assertLess(plan.stored_surplus_kwh, 0.5)
        self.assertLessEqual(plan.recommended_export_w, 300)

    def test_low_soc_still_charges_before_exporting(self):
        plan = self._plan(soc=30.0, today_pv_w=800, tomorrow_peak_w=4500)
        self.assertGreater(plan.battery_input_kwh_needed, 0.0)
        self.assertEqual(plan.stored_surplus_kwh, 0.0)
        self.assertEqual(plan.recommended_export_w, 0)

    def test_missing_tomorrow_forecast_never_releases_stored_energy(self):
        now = dt.datetime(2026, 9, 5, 12, 51, tzinfo=self.tz)
        sunset = dt.datetime(2026, 9, 5, 20, 6, tzinfo=self.tz)
        day = self._day(now, sunset, 800)
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            db.set("last_full_balance_at", (now - dt.timedelta(days=1)).isoformat(), now)
            plan = build_day_energy_plan(self.raw(), db, day, now, 99.0, None)
            db.close()
        self.assertEqual(plan.end_of_day_target_reason, "hold_day_target_forecast_cannot_refill")
        self.assertLess(plan.stored_surplus_kwh, 0.5)

    def test_save_strategy_never_exports_stored_surplus(self):
        raw = self.raw()
        raw["strategy"] = {"active": "save"}
        plan = self._plan(soc=99.0, today_pv_w=800, tomorrow_peak_w=4500, raw=raw)
        self.assertEqual(plan.recommended_export_w, 0)
        self.assertEqual(plan.allocation_mode, "strategy_save")

    def test_abundant_budget_allocates_at_the_hard_cap(self):
        plan = self._plan(soc=99.0, today_pv_w=3000, tomorrow_peak_w=4500)
        self.assertGreaterEqual(plan.cap_sustain_hours, plan.hours_to_sunset - 0.01)
        self.assertEqual(plan.allocation_mode, "cap_sustained")
        self.assertEqual(plan.recommended_export_w, 1000)

    def test_partial_budget_falls_back_to_flat_average(self):
        """A budget that cannot sustain the cap for the whole window is still spread."""
        plan = self._plan(soc=64.0, today_pv_w=1000, tomorrow_peak_w=4500)
        self.assertEqual(plan.allocation_mode, "flat_average")
        self.assertGreater(plan.recommended_export_w, 0)
        self.assertLess(plan.recommended_export_w, 1000)


class IntradayBiasTests(unittest.TestCase):
    """v3.1.2: correct the remaining forecast by how today is actually tracking."""

    tz = ZoneInfo("Europe/Amsterdam")

    def raw(self, **over):
        r = EnergyStrategyTests.raw(self)
        r["day_strategy"] = dict(r["day_strategy"], **{
            "intraday_bias_enabled": True, "intraday_bias_window_hours": 3.0,
            "intraday_bias_min_forecast_kwh": 1.0, "intraday_bias_trust_kwh": 3.0,
            "intraday_bias_min": 0.40, "intraday_bias_max": 1.60, **over})
        return r

    def _day(self, date, w=2000):
        """Flat PV so the forecast integral is trivially checkable: 2 kW = 0.5 kWh/15min."""
        start = dt.datetime.combine(date, dt.time(6, 0), tzinfo=self.tz)
        pts = [SimpleNamespace(time=start + dt.timedelta(minutes=15 * i), predicted_w=w) for i in range(56)]
        return SimpleNamespace(date=date, sunrise=start, sunset=start + dt.timedelta(hours=14), points=pts)

    def _db(self, td, samples):
        db = StateDB(str(Path(td) / "state.db"))
        for at, kwh in samples:
            db.add_device_telemetry(
                at, at,
                {"SOC": "90", "TotalSolarPower": "2000", "BatteryPower": "0",
                 "TotalGridPower": "0", "TotalConsumptionPower": "300",
                 "DailyActiveProduction": str(kwh)},
                control_soc=90.0, soc_confidence="FRESH",
                telemetry_age_minutes=1.0, raw_json={},
            )
        return db

    def test_overperforming_day_raises_the_remaining_forecast(self):
        from energy_strategy import intraday_forecast_bias
        date = dt.date(2026, 9, 5)
        day = self._day(date)
        now = dt.datetime.combine(date, dt.time(12, 0), tzinfo=self.tz)
        with tempfile.TemporaryDirectory() as td:
            # 06:00-12:00 forecast = 12.0 kWh; actual 14.4 kWh => ratio 1.2
            db = self._db(td, [(now - dt.timedelta(hours=6), 0.0), (now, 14.4)])
            bias, src = intraday_forecast_bias(self.raw(), db, day, now)
            db.close()
        self.assertGreater(bias, 1.0)
        self.assertLessEqual(bias, 1.6)
        self.assertIn("intraday_", src)

    def test_underperforming_day_lowers_the_remaining_forecast(self):
        from energy_strategy import intraday_forecast_bias
        date = dt.date(2026, 9, 5)
        day = self._day(date)
        now = dt.datetime.combine(date, dt.time(12, 0), tzinfo=self.tz)
        with tempfile.TemporaryDirectory() as td:
            db = self._db(td, [(now - dt.timedelta(hours=6), 0.0), (now, 3.0)])
            bias, _ = intraday_forecast_bias(self.raw(), db, day, now)
            db.close()
        self.assertLess(bias, 1.0)
        self.assertGreaterEqual(bias, 0.40)

    def test_stale_telemetry_does_not_invent_a_shortfall(self):
        """The forecast must be integrated to the sample time, not to the wall clock."""
        from energy_strategy import intraday_forecast_bias
        date = dt.date(2026, 9, 5)
        day = self._day(date)
        sample_at = dt.datetime.combine(date, dt.time(12, 0), tzinfo=self.tz)
        now = sample_at + dt.timedelta(hours=3)  # cloud went dark for three hours
        with tempfile.TemporaryDirectory() as td:
            # Actual exactly matches the forecast up to the sample: ratio must be 1.0.
            db = self._db(td, [(sample_at - dt.timedelta(hours=6), 0.0), (sample_at, 12.0)])
            bias, src = intraday_forecast_bias(self.raw(), db, day, now)
            db.close()
        self.assertAlmostEqual(bias, 1.0, places=2)
        self.assertIn("stale", src)

    def test_correction_is_shrunk_before_enough_forecast_has_elapsed(self):
        from energy_strategy import intraday_forecast_bias
        date = dt.date(2026, 9, 5)
        day = self._day(date)
        # Only 30 minutes after sunrise: 1.0 kWh forecast, half-delivered.
        now = dt.datetime.combine(date, dt.time(6, 30), tzinfo=self.tz)
        with tempfile.TemporaryDirectory() as td:
            db = self._db(td, [(now, 0.5)])
            bias, src = intraday_forecast_bias(self.raw(), db, day, now)
            db.close()
        # Raw ratio is 0.5; shrinkage must keep it far closer to 1.0 this early.
        self.assertGreater(bias, 0.75)

    def test_disabled_flag_is_inert(self):
        from energy_strategy import intraday_forecast_bias
        date = dt.date(2026, 9, 5)
        day = self._day(date)
        now = dt.datetime.combine(date, dt.time(12, 0), tzinfo=self.tz)
        with tempfile.TemporaryDirectory() as td:
            db = self._db(td, [(now - dt.timedelta(hours=6), 0.0), (now, 3.0)])
            bias, src = intraday_forecast_bias(self.raw(intraday_bias_enabled=False), db, day, now)
            db.close()
        self.assertEqual(bias, 1.0)
        self.assertEqual(src, "intraday_disabled")

    def test_no_actuals_yet_is_neutral(self):
        from energy_strategy import intraday_forecast_bias
        date = dt.date(2026, 9, 5)
        day = self._day(date)
        now = dt.datetime.combine(date, dt.time(12, 0), tzinfo=self.tz)
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            bias, src = intraday_forecast_bias(self.raw(), db, day, now)
            db.close()
        self.assertEqual(bias, 1.0)
        self.assertEqual(src, "intraday_no_actuals")


class LoadScaledReserveTests(unittest.TestCase):
    """v3.1.3: the reserve is hours of house cover, not a fixed slab of kWh."""

    tz = ZoneInfo("Europe/Amsterdam")

    def raw(self, house_w, **over):
        r = EnergyStrategyTests.raw(self)
        # Pin the learned load by making the fallback the only available value.
        r["load_forecast"] = dict(r["load_forecast"], fallback_house_load_w=house_w,
                                  min_house_load_w=50.0, max_house_load_w=2000.0,
                                  min_learning_days=99)
        r["day_strategy"] = dict(r["day_strategy"], **{
            "load_scaled_reserve_enabled": True,
            "reserve_min_kwh": 0.5, "reserve_max_kwh": 6.0, **over})
        return r

    def _reserve(self, house_w, tag="conservative", **over):
        from energy_strategy import strategy_reserve_kwh
        from strategy_presets import PRESETS
        now = dt.datetime(2026, 9, 7, 12, 0, tzinfo=self.tz)
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            try:
                return strategy_reserve_kwh(self.raw(house_w, **over), db, now, PRESETS[tag])
            finally:
                db.close()

    def test_present_load_reproduces_the_old_fixed_reserve(self):
        """At today's 250 W learned house load the conservative reserve is unchanged."""
        kwh, src = self._reserve(250.0)
        self.assertAlmostEqual(kwh, 2.47, places=2)   # was a flat 2.50
        self.assertIn("load_scaled", src)

    def test_heating_season_load_raises_the_reserve(self):
        summer, _ = self._reserve(150.0)
        winter, _ = self._reserve(700.0)
        self.assertLess(summer, 2.0)
        self.assertGreater(winter, 5.0)
        self.assertGreater(winter, summer * 2)

    def test_reserve_is_clamped_at_both_ends(self):
        # learned_house_load_w has its own 50 W floor, so the lower clamp only bites
        # when the configured minimum is raised above what that load implies.
        tiny, src_lo = self._reserve(50.0, reserve_min_kwh=2.0)
        huge, src_hi = self._reserve(1900.0)
        self.assertEqual(tiny, 2.0)
        self.assertIn("clamped", src_lo)
        self.assertEqual(huge, 6.0)
        self.assertIn("clamped", src_hi)

    def test_max_export_keeps_a_zero_reserve(self):
        kwh, src = self._reserve(700.0, tag="max-export")
        self.assertEqual(kwh, 0.0)
        self.assertEqual(src, "fixed_no_hours")

    def test_disabled_flag_restores_the_fixed_reserve(self):
        kwh, src = self._reserve(700.0, load_scaled_reserve_enabled=False)
        self.assertEqual(kwh, 2.50)
        self.assertEqual(src, "fixed")

    def test_save_strategy_reserves_more_than_conservative_at_any_load(self):
        for load in (150.0, 250.0, 500.0):
            c, _ = self._reserve(load, tag="conservative")
            s, _ = self._reserve(load, tag="save")
            self.assertGreater(s, c, f"load {load}")
