import datetime as dt
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from controller import Config, Controller
from deye_api import DeyeAPIError
from state_db import StateDB


class NullLog:
    def __init__(self):
        self.events = []

    def emit(self, level, event, **fields):
        self.events.append((level, event, fields))


class FakeClient:
    def __init__(self, mode):
        self.mode = mode
        self.write_calls = 0
        self.dynamic_read_calls = 0

    def set_max_sell_power(self, sn, watts):
        self.write_calls += 1
        if self.mode == "offline":
            raise DeyeAPIError("MAX_SELL_POWER update failed: 2104006 device offline", 2104006)
        if self.mode == "busy":
            raise DeyeAPIError("MAX_SELL_POWER update failed: 2104004 command concurrent running", 2104004)
        if self.mode == "timeout":
            raise DeyeAPIError("Deye API timeout")
        return {"success": True, "orderId": 123456}

    def dynamic_control(self, sn, *, max_sell_power=None):
        raise AssertionError("dynamic_control write not expected in this test")

    def dynamic_control_read(self, sn):
        self.dynamic_read_calls += 1
        raise AssertionError("v3.0.0 controller must not issue a preliminary dynamic read")


class DirectWriteTests(unittest.TestCase):
    def make_controller(self, td, mode="accepted", *, legacy_probe="dynamic_read"):
        cfg = Config({
            "site": {"timezone": "Europe/Vilnius"},
            "deye": {"inverter_sn": "TEST_INVERTER_SN"},
            "grid": {"export_hard_limit_w": 1000},
            "battery": {"soc_floor_pct": 15.0, "effective_kwh": 15.0},
            "control": {
                "dry_run": False,
                "write_api": "power_update",
                # Existing v2.0.5 installs may still carry this legacy key.
                "control_probe_mode": legacy_probe,
                "offline_retry_seconds": 60,
                "uncertain_submit_guard_minutes": 120,
                "max_writes_per_day": 4,
                "max_successful_writes_per_day": 4,
                "bonus_write_enabled": True,
                "bonus_write_delta_w": 500,
                "max_successful_writes_with_bonus": 5,
                "max_order_submissions_per_day": 8,
                "min_write_interval_minutes": 0,
                "min_write_delta_w": 200,
                "order_status_poll_seconds": 15,
            },
        })
        ctl = Controller.__new__(Controller)
        ctl.cfg = cfg
        ctl.tz = ZoneInfo("Europe/Vilnius")
        ctl.db = StateDB(str(Path(td) / "state.db"))
        ctl.log = NullLog()
        ctl.client = FakeClient(mode)
        ctl.current_setting_w = 1000
        ctl.active_order = None
        ctl.active_context = {}
        ctl.next_control_attempt_at = None
        ctl.next_order_poll_at = None
        return ctl

    def test_legacy_dynamic_probe_config_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_controller(td, "accepted", legacy_probe="dynamic_read")
            now = dt.datetime(2026, 9, 2, 2, 0, tzinfo=ctl.tz)
            action = ctl.maybe_control(now, 100, "NIGHT", "night_plan", 26.0, now + dt.timedelta(hours=4), "FRESH", 1.0)
            self.assertEqual(action, "direct_accepted")
            self.assertEqual(ctl.client.write_calls, 1)
            self.assertEqual(ctl.client.dynamic_read_calls, 0)
            self.assertIsNotNone(ctl.active_order)
            self.assertEqual(ctl.db.writes_since(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()), 1)
            ctl.db.close()

    def test_offline_rejection_is_not_counted_as_write(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_controller(td, "offline")
            now = dt.datetime(2026, 9, 2, 2, 0, tzinfo=ctl.tz)
            result = ctl.submit_write(now, 100, "night_plan", {})
            self.assertEqual(result, "offline")
            self.assertEqual(ctl.db.writes_since(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()), 0)
            self.assertEqual(ctl.db.get("control_path_state"), "OFFLINE")
            ctl.db.close()

    def test_busy_rejection_is_not_counted_as_write(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_controller(td, "busy")
            now = dt.datetime(2026, 9, 2, 2, 0, tzinfo=ctl.tz)
            result = ctl.submit_write(now, 100, "night_plan", {})
            self.assertEqual(result, "busy")
            self.assertEqual(ctl.db.writes_since(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()), 0)
            self.assertEqual(ctl.db.get("control_path_state"), "BUSY")
            ctl.db.close()

    def test_ambiguous_timeout_sets_no_retry_guard(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_controller(td, "timeout")
            now = dt.datetime(2026, 9, 2, 2, 0, tzinfo=ctl.tz)
            result = ctl.submit_write(now, 100, "night_plan", {})
            self.assertEqual(result, "uncertain")
            self.assertEqual(ctl.db.writes_since(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()), 0)
            guard = dt.datetime.fromisoformat(ctl.db.get("uncertain_write_guard_until"))
            self.assertEqual(guard, now + dt.timedelta(minutes=120))
            allowed, why = ctl.can_write(now + dt.timedelta(minutes=1), 100, "night_plan")
            self.assertFalse(allowed)
            self.assertIn("uncertain-submit guard", why)
            ctl.db.close()

    def test_confirmed_failed_order_uses_short_backoff_not_120min_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_controller(td, "accepted")
            base = dt.datetime(2026, 9, 2, 3, 18, tzinfo=ctl.tz)
            ctl.db.add_write(base, 1000, 0, "night_plan", 999, "failed", "status=500", accepted=True)
            ctl.db.set("failed_order_retry_after", (base + dt.timedelta(minutes=15)).isoformat(), base)
            # No successful write exists. One minute after the confirmed-failure
            # backoff expires, the old 120-minute accepted-write cooldown must not block.
            allowed, why = ctl.can_write(base + dt.timedelta(minutes=16), 0, "night_plan")
            self.assertTrue(allowed, why)
            ctl.db.close()


    def test_four_successes_allow_only_large_bonus_fifth(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_controller(td, "accepted")
            base = dt.datetime(2026, 9, 4, 12, 0, tzinfo=ctl.tz)
            for i, target in enumerate((800, 600, 800, 1000), start=1):
                t = base + dt.timedelta(minutes=i)
                ctl.db.add_write(t, 1000, target, "test", 1000+i, "success", "confirmed", accepted=True)
            ctl.current_setting_w = 1000
            allowed_small, why_small = ctl.can_write(base + dt.timedelta(hours=1), 700, "morning_day_restore")
            self.assertFalse(allowed_small)
            self.assertIn("bonus needs", why_small)
            allowed_big, why_big = ctl.can_write(base + dt.timedelta(hours=1), 400, "morning_day_restore")
            self.assertTrue(allowed_big, why_big)
            ctl.db.close()

    def test_failed_orders_do_not_use_success_budget_but_submission_budget_still_applies(self):
        with tempfile.TemporaryDirectory() as td:
            ctl = self.make_controller(td, "accepted")
            base = dt.datetime(2026, 9, 4, 12, 0, tzinfo=ctl.tz)
            for i in range(8):
                t = base + dt.timedelta(minutes=i)
                ctl.db.add_write(t, 1000, 0, "test", 2000+i, "failed", "status=500", accepted=True)
            self.assertEqual(ctl.writes_today(base + dt.timedelta(hours=1)), 0)
            self.assertEqual(ctl.order_submissions_today(base + dt.timedelta(hours=1)), 8)
            allowed, why = ctl.can_write(base + dt.timedelta(hours=1), 0, "morning_day_restore")
            self.assertFalse(allowed)
            self.assertIn("submission safety budget", why)
            ctl.db.close()


if __name__ == "__main__":
    unittest.main()


class DeviceRejectBackoffTests(unittest.TestCase):
    """v3.1.1: Deye error 540 is a device rejection, not a transient cloud failure."""

    def make_controller(self, td, status):
        ctl = DirectWriteTests.make_controller(self, td)
        ctl.cfg.raw["control"].update({
            "failed_order_retry_minutes": 15,
            "device_reject_retry_minutes": 45,
            "device_reject_error_codes": "540",
        })

        class OrderClient:
            def check_order_status(self_inner, order_id):
                return status

        ctl.client = OrderClient()
        return ctl

    def _fail_once(self, td, status):
        ctl = self.make_controller(td, status)
        now = dt.datetime(2026, 9, 5, 9, 41, tzinfo=ctl.tz)
        ctl.db.add_write(now, 400, 100, "day_energy_budget", 999, "accepted", "ok", accepted=True)
        ctl.active_order = {"order_id": 999, "requested_w": 100}
        ctl.active_context = {}
        result = ctl.poll_active_order(now)
        blocked_until = dt.datetime.fromisoformat(ctl.db.get("failed_order_retry_after"))
        ctl.db.close()
        return result, (blocked_until - now).total_seconds() / 60.0

    def test_error_540_uses_the_long_device_reject_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            result, minutes = self._fail_once(td, {"status": 500, "error": "540", "orderId": 999})
            self.assertEqual(result, "failed")
            self.assertAlmostEqual(minutes, 45.0, places=3)

    def test_other_failures_keep_the_normal_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            result, minutes = self._fail_once(td, {"status": 500, "error": "999", "orderId": 999})
            self.assertEqual(result, "failed")
            self.assertAlmostEqual(minutes, 15.0, places=3)
