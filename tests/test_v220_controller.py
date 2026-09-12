import datetime as dt
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from controller import Config, Controller, DeviceSnapshot
from state_db import StateDB


class NullLog:
    def __init__(self):
        self.events = []
    def emit(self, level, event, **fields):
        self.events.append((level, event, fields))


class V220ControllerTests(unittest.TestCase):
    def make_ctl(self, td):
        ctl = Controller.__new__(Controller)
        ctl.cfg = Config({
            "site": {"timezone": "Europe/Amsterdam"},
            "grid": {"export_hard_limit_w": 1000, "day_export_w": 1000},
            "battery": {"effective_kwh": 15.0, "soc_floor_pct": 15.0, "day_target_soc_pct": 96.0},
            "control": {"max_writes_per_day": 4, "min_write_interval_minutes": 120, "min_write_delta_w": 200},
            "day_strategy": {"max_budget_writes_per_day": 2},
            "telemetry_health": {"stale_warning_minutes": 10, "cloud_offline_minutes": 20},
        })
        ctl.tz = ZoneInfo("Europe/Amsterdam")
        ctl.db = StateDB(str(Path(td) / "state.db"))
        ctl.log = NullLog()
        ctl.current_setting_w = 1000
        ctl.active_order = None
        ctl.active_context = {}
        ctl.next_control_attempt_at = None
        ctl.next_order_poll_at = None
        return ctl

    def test_cloud_stall_and_recovery_are_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 4, 12, 0, tzinfo=ctl.tz)
            snap = DeviceSnapshot(
                collection_at=now - dt.timedelta(minutes=25),
                device_state=3,
                metrics={"SOC": "60", "TotalSolarPower": "2000", "BatteryPower": "-500", "TotalGridPower": "-1000", "TotalConsumptionPower": "100", "AC Temperature": "43.5"},
                raw_response={},
            )
            state = ctl.update_telemetry_health(now, snap, 25.0, False)
            self.assertEqual(state, "CLOUD_OFFLINE")
            self.assertEqual(ctl.db.get("telemetry_health_state"), "CLOUD_OFFLINE")
            self.assertIsNotNone(ctl.db.get("telemetry_stall_incident"))

            recovered = DeviceSnapshot(
                collection_at=now + dt.timedelta(minutes=1),
                device_state=1,
                metrics={"SOC": "61", "TotalSolarPower": "2500"},
                raw_response={},
            )
            state2 = ctl.update_telemetry_health(now + dt.timedelta(minutes=1), recovered, 0.5, True)
            self.assertEqual(state2, "RECOVERED")
            self.assertIsNotNone(ctl.db.get("telemetry_last_recovered_at"))
            ctl.db.close()

    def test_day_budget_caps_two_accepted_adjustments(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 4, 14, 0, tzinfo=ctl.tz)
            ctl.db.add_write(now - dt.timedelta(hours=5), 1000, 600, "day_energy_budget", 123, "success", "ok", accepted=True)
            ctl.db.add_write(now - dt.timedelta(hours=3), 600, 1000, "day_energy_budget", 124, "success", "ok", accepted=True)
            allowed, why = ctl.can_write(now, 200, "day_energy_budget")
            self.assertFalse(allowed)
            self.assertIn("day energy-budget", why)
            ctl.db.close()

    def test_all_control_writes_freeze_when_cloud_is_over_20min_old(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 4, 14, 0, tzinfo=ctl.tz)
            action = ctl.maybe_control(now, 600, "DAY", "day_energy_budget", 70.0, None, "STALE", 21.0)
            self.assertEqual(action, "blocked_cloud_offline")
            ctl.db.close()

    def test_strategy_replan_can_escape_old_15pct_night_trajectory(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 4, 0, 40, tzinfo=ctl.tz)
            target_time = dt.datetime(2026, 9, 4, 7, 50, tzinfo=ctl.tz)
            ctl.db.set("night_plan", {
                "cycle": "2026-09-04",
                "start": (now - dt.timedelta(hours=2)).isoformat(),
                "start_soc": 80.0,
                "target_time": target_time.isoformat(),
                "target_soc": 15.0,
                "corrections": 1,
            }, now)
            ctl.db.set("current_night_target_soc", 90.0, now)
            self.assertTrue(ctl.correction_allowed(now, 64.0, target_time))
            ctl.db.close()


if __name__ == "__main__":
    unittest.main()


class CurtailmentOverrideTests(unittest.TestCase):
    """v3.1.1: a full battery with PV still producing must reopen the sell cap."""

    def make_ctl(self, td):
        ctl = V220ControllerTests.make_ctl(self, td)
        ctl.cfg.raw["day_strategy"].update({
            "curtailment_override_enabled": True,
            "curtailment_override_soc_pct": 98.0,
            "curtailment_override_charge_w": 300.0,
            "curtailment_override_min_pv_w": 100.0,
            "max_telemetry_age_minutes": 15.0,
        })
        return ctl

    def snap(self, ctl, now, *, soc, pv_w, battery_w, age_min=2.0):
        return DeviceSnapshot(
            collection_at=now - dt.timedelta(minutes=age_min),
            device_state=1,
            metrics={
                "SOC": str(soc),
                "TotalSolarPower": str(pv_w),
                "BatteryPower": str(battery_w),
                "TotalGridPower": "-399",
                "TotalConsumptionPower": "2059",
            },
            raw_response={},
        )

    def test_full_battery_not_absorbing_reopens_the_cap(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 5, 12, 51, tzinfo=ctl.tz)
            # Battery at its ceiling, PV producing, battery power positive
            # (discharging) => nothing is absorbing the surplus.
            snap = self.snap(ctl, now, soc=99.0, pv_w=2000, battery_w=800)
            got, reason = ctl.curtailment_override(now, snap, 0, 1000)
            self.assertEqual(got, 1000)
            self.assertEqual(reason, "curtailment_override_battery_full")
            ctl.db.close()

    def test_battery_still_charging_is_not_curtailing(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 5, 12, 51, tzinfo=ctl.tz)
            # Negative battery power == charging at 2 kW, so surplus has somewhere to go.
            snap = self.snap(ctl, now, soc=99.0, pv_w=4000, battery_w=-2000)
            self.assertEqual(ctl.curtailment_override(now, snap, 0, 1000)[0], None)
            ctl.db.close()

    def test_stale_telemetry_never_opens_the_valve(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 5, 12, 51, tzinfo=ctl.tz)
            snap = self.snap(ctl, now, soc=99.0, pv_w=2000, battery_w=800, age_min=40.0)
            self.assertEqual(ctl.curtailment_override(now, snap, 0, 1000)[0], None)
            ctl.db.close()

    def test_no_override_below_the_soc_threshold_or_without_pv(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 5, 12, 51, tzinfo=ctl.tz)
            low = self.snap(ctl, now, soc=80.0, pv_w=2000, battery_w=800)
            dark = self.snap(ctl, now, soc=99.0, pv_w=0, battery_w=800)
            self.assertEqual(ctl.curtailment_override(now, low, 0, 1000)[0], None)
            self.assertEqual(ctl.curtailment_override(now, dark, 0, 1000)[0], None)
            ctl.db.close()

    def test_override_never_lowers_an_already_open_cap(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 5, 12, 51, tzinfo=ctl.tz)
            snap = self.snap(ctl, now, soc=99.0, pv_w=2000, battery_w=800)
            self.assertEqual(ctl.curtailment_override(now, snap, 1000, 1000)[0], None)
            ctl.db.close()

    def test_disabled_override_is_inert(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            ctl.cfg.raw["day_strategy"]["curtailment_override_enabled"] = False
            now = dt.datetime(2026, 9, 5, 12, 51, tzinfo=ctl.tz)
            snap = self.snap(ctl, now, soc=99.0, pv_w=2000, battery_w=800)
            self.assertEqual(ctl.curtailment_override(now, snap, 0, 1000)[0], None)
            ctl.db.close()


class MorningRampTests(unittest.TestCase):
    """v3.1.9: don't spend the day's write budget chasing a settling recommendation."""

    def make_ctl(self, td):
        ctl = V220ControllerTests.make_ctl(self, td)
        ctl.cfg.raw["day_strategy"].update({
            "morning_settle_minutes": 40, "max_morning_writes_per_day": 1,
            "max_budget_writes_per_day": 2,
        })
        ctl.cfg.raw["control"].update({
            "max_successful_writes_per_day": 4, "max_order_submissions_per_day": 8,
            "min_write_interval_minutes": 0, "min_write_delta_w": 200,
            "bonus_write_enabled": False,
        })
        return ctl

    def test_writes_are_held_until_the_recommendation_settles(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            handoff = dt.datetime(2026, 9, 12, 8, 50, tzinfo=ctl.tz)
            ctl.db.set("morning_settle_until", (handoff + dt.timedelta(minutes=40)).isoformat(), handoff)
            ctl.current_setting_w = 0
            # The 400 -> 600 -> 1000 ramp all falls inside the settle window.
            for offset in (0, 35, 38):
                ok, why = ctl.can_write(handoff + dt.timedelta(minutes=offset), 1000, "morning_day_restore")
                self.assertFalse(ok, f"+{offset} min should be held")
                self.assertIn("settling", why)
            ok, why = ctl.can_write(handoff + dt.timedelta(minutes=41), 1000, "morning_day_restore")
            self.assertTrue(ok, why)
            ctl.db.close()

    def test_only_one_morning_write_per_day(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 12, 9, 40, tzinfo=ctl.tz)
            ctl.db.set("morning_settle_until", (now - dt.timedelta(minutes=10)).isoformat(), now)
            ctl.current_setting_w = 0
            self.assertTrue(ctl.can_write(now, 1000, "morning_day_restore")[0])
            ctl.db.add_write(now, 0, 1000, "morning_day_restore", 1, "success", "ok", accepted=True)
            ok, why = ctl.can_write(now + dt.timedelta(minutes=5), 600, "morning_day_restore")
            self.assertFalse(ok)
            self.assertIn("morning-restore", why)
            ctl.db.close()

    def test_the_afternoon_budget_is_not_consumed_by_the_morning(self):
        """The point of the change: leave writes for when they matter."""
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 12, 9, 40, tzinfo=ctl.tz)
            ctl.current_setting_w = 1000
            ctl.db.add_write(now, 0, 1000, "morning_day_restore", 1, "success", "ok", accepted=True)
            ok, why = ctl.can_write(now + dt.timedelta(hours=5), 300, "day_energy_budget")
            self.assertTrue(ok, why)
            ctl.db.close()

    def test_settling_does_not_gate_other_reasons(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_ctl(td)
            now = dt.datetime(2026, 9, 12, 9, 0, tzinfo=ctl.tz)
            ctl.db.set("morning_settle_until", (now + dt.timedelta(minutes=30)).isoformat(), now)
            ctl.current_setting_w = 0
            self.assertTrue(ctl.can_write(now, 1000, "day_energy_budget")[0])
            ctl.db.close()
