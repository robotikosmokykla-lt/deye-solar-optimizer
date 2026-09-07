import datetime as dt
import unittest
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from controller import (
    Config,
    DeviceSnapshot,
    calculate_night_export_w,
    control_pv_wakeup,
    control_response_accepted,
    derive_control_soc,
    expected_soc_on_linear_plan,
    quantize_w,
)


class MathTests(unittest.TestCase):
    def cfg(self):
        return Config({
            "site": {"timezone": "Europe/Vilnius"},
            "battery": {"soc_floor_pct": 15.0, "effective_kwh": 15.0},
            "load_model": {"base_house_load_w": 115, "system_overhead_w": 120},
            "grid": {"export_hard_limit_w": 1000},
            "control": {
                "write_step_w": 100,
                "soc_fresh_minutes": 5,
                "soc_estimate_max_minutes": 75,
                "soc_estimate_max_pv_w": 100,
                "max_control_telemetry_age_minutes": 90,
            },
            "pv": {"wakeup_bias_minutes": 15},
            "night_strategy": {"morning_surplus_threshold_w": 350, "sustained_minutes": 30, "floor_lead_minutes": 10},
        })

    def test_quantize_hard_limit(self):
        self.assertEqual(quantize_w(1450, 100, 1000), 1000)
        self.assertEqual(quantize_w(-200, 100, 1000), 0)

    def test_sep1_like_case(self):
        tz = ZoneInfo("Europe/Vilnius")
        now = dt.datetime(2026, 9, 1, 22, 5, tzinfo=tz)
        target = dt.datetime(2026, 9, 2, 6, 47, tzinfo=tz)
        self.assertEqual(calculate_night_export_w(self.cfg(), 56, now, target), 500)

    def test_control_wakeup_uses_sustained_useful_pv(self):
        tz = ZoneInfo("Europe/Vilnius")
        sunrise = dt.datetime(2026, 9, 2, 6, 32, tzinfo=tz)
        points = [
            SimpleNamespace(time=dt.datetime(2026, 9, 2, 6, 45, tzinfo=tz), predicted_w=200),
            SimpleNamespace(time=dt.datetime(2026, 9, 2, 7, 0, tzinfo=tz), predicted_w=400),
            SimpleNamespace(time=dt.datetime(2026, 9, 2, 7, 15, tzinfo=tz), predicted_w=450),
        ]
        fc = SimpleNamespace(
            sunrise=sunrise,
            pv_wakeup=dt.datetime(2026, 9, 2, 6, 45, tzinfo=tz),
            useful_pv_start=dt.datetime(2026, 9, 2, 7, 0, tzinfo=tz),
            points=points,
        )
        # 07:00 first sustained point +15min site bias -10min lead = 07:05.
        self.assertEqual(control_pv_wakeup(self.cfg(), fc), dt.datetime(2026, 9, 2, 7, 5, tzinfo=tz))

    def test_stale_night_soc_estimate_matches_observed_case(self):
        tz = ZoneInfo("Europe/Vilnius")
        collection = dt.datetime(2026, 9, 2, 0, 21, 3, tzinfo=tz)
        now = dt.datetime(2026, 9, 2, 1, 11, 17, tzinfo=tz)
        snap = DeviceSnapshot(
            collection_at=collection,
            device_state=3,
            metrics={"SOC": "39", "BatteryPower": "1209", "TotalSolarPower": "0.00"},
            raw_response={},
        )
        soc, confidence, age = derive_control_soc(self.cfg(), snap, now)
        self.assertEqual(confidence, "ESTIMATED")
        self.assertAlmostEqual(age, 50.233, places=2)
        self.assertAlmostEqual(soc, 32.25, places=1)

    def test_soc_extrapolation_can_be_disabled_after_control_change(self):
        tz = ZoneInfo("Europe/Vilnius")
        collection = dt.datetime(2026, 9, 2, 0, 21, 3, tzinfo=tz)
        now = dt.datetime(2026, 9, 2, 1, 11, 17, tzinfo=tz)
        snap = DeviceSnapshot(
            collection_at=collection,
            device_state=3,
            metrics={"SOC": "39", "BatteryPower": "1209", "TotalSolarPower": "0.00"},
            raw_response={},
        )
        soc, confidence, _ = derive_control_soc(self.cfg(), snap, now, allow_power_extrapolation=False)
        self.assertEqual(confidence, "STALE")
        self.assertEqual(soc, 39.0)

    def test_dynamic_response_offline_is_not_accepted(self):
        accepted, order_id, why = control_response_accepted({"success": True, "connectionStatus": 0, "orderId": 0})
        self.assertFalse(accepted)
        self.assertIsNone(order_id)
        self.assertEqual(why, "device_offline")

    def test_dynamic_response_order_is_accepted(self):
        accepted, order_id, why = control_response_accepted({"success": True, "connectionStatus": 1, "orderId": 12345})
        self.assertTrue(accepted)
        self.assertEqual(order_id, 12345)
        self.assertEqual(why, "accepted")

    def test_linear_soc(self):
        tz = ZoneInfo("Europe/Vilnius")
        s = dt.datetime(2026, 9, 1, 22, 0, tzinfo=tz)
        t = dt.datetime(2026, 9, 2, 6, 0, tzinfo=tz)
        mid = dt.datetime(2026, 9, 2, 2, 0, tzinfo=tz)
        self.assertAlmostEqual(expected_soc_on_linear_plan(55, 15, s, t, mid), 35.0)


if __name__ == "__main__":
    unittest.main()
