import datetime as dt
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from controller import Config, Controller
from profile_manager import normalize_mode, normalize_pattern, PROFILES, apply_profile


class ProfileTests(unittest.TestCase):
    def test_profile_mapping(self):
        self.assertEqual(PROFILES["export-first"]["work_mode"], "SELLING_FIRST")
        self.assertEqual(PROFILES["self-consumption"]["work_mode"], "ZERO_EXPORT_TO_CT")
        self.assertEqual(PROFILES["self-consumption"]["energy_pattern"], "LOAD_FIRST")

    def test_normalization(self):
        self.assertEqual(normalize_mode("SELL_FIRST"), "SELLING_FIRST")
        self.assertEqual(normalize_mode("zero export to ct"), "ZERO_EXPORT_TO_CT")
        self.assertEqual(normalize_pattern("BattFirst"), "BATTERY_FIRST")
        self.assertEqual(normalize_pattern("LoadFirst"), "LOAD_FIRST")

    def test_export_first_dry_run_does_not_require_config_system(self):
        class FakeClient:
            def get_system_config(self, sn):
                raise RuntimeError("2106002 data upload failed")

            def get_device_latest(self, sn):
                return {
                    "deviceDataList": [{
                        "collectionTime": 1788300677,
                        "deviceState": 3,
                        "dataList": [
                            {"key": "SOC", "value": "80"},
                            {"key": "TotalSolarPower", "value": "8000"},
                            {"key": "TotalGridPower", "value": "-500"},
                            {"key": "BatteryPower", "value": "-7300"},
                            {"key": "TotalConsumptionPower", "value": "80"},
                        ],
                    }]
                }

        with tempfile.TemporaryDirectory() as td:
            cfg = {
                "deye": {"inverter_sn": "2512"},
                "site": {"timezone": "Europe/Vilnius"},
                "grid": {"export_hard_limit_w": 1000},
                "logging": {"state_db": str(Path(td) / "state.db")},
            }
            with patch("profile_manager.make_client", return_value=FakeClient()):
                rc = apply_profile(cfg, "export-first", live=False, observe_minutes=0)
            self.assertEqual(rc, 0)

    def test_self_consumption_profile_disables_night_export_planning(self):
        with tempfile.TemporaryDirectory() as td:
            state_db = str(Path(td) / "state.db")
            Path(td, "profile-state.json").write_text(json.dumps({"profile": "self-consumption"}))
            ctl = Controller.__new__(Controller)
            ctl.cfg = Config({
                "grid": {"day_export_w": 1000},
                "battery": {"soc_floor_pct": 15.0},
                "logging": {"state_db": state_db},
            })
            phase, target, target_time, reason = ctl.classify_and_decide(
                dt.datetime(2026, 10, 10, 2, 0, tzinfo=dt.timezone.utc), 80.0
            )
            self.assertEqual(phase, "SELF_CONSUMPTION")
            self.assertEqual(target, 1000)
            self.assertIsNone(target_time)
            self.assertEqual(reason, "profile_self_consumption")


if __name__ == "__main__":
    unittest.main()
