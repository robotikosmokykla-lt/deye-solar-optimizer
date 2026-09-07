import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config_loader import load_config, redact_env_lines


class EnvConfigTests(unittest.TestCase):
    def test_env_loads_site_pv_appliances_and_write_budgets(self):
        arrays = [{"name":"roof","kwp":10.5,"tilt_deg":35,"azimuth_deg":15}]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "deye.env"
            p.write_text("\n".join([
                'DEYE_APP_ID="id"',
                'DEYE_APP_SECRET="secret"',
                'DEYE_LOGIN="mail@example.com"',
                'DEYE_PASSWORD="pw#1"',
                'DEYE_INVERTER_SN="123"',
                'SITE_LATITUDE=55.1',
                'SITE_LONGITUDE=23.9',
                'GRID_EXPORT_HARD_LIMIT_W=900',
                'GRID_DAY_EXPORT_W=900',
                'PV_ARRAYS_JSON=' + json.dumps(json.dumps(arrays, separators=(",",":"))),
                'WATER_HEATER_TIME="09:15"',
                'WATER_HEATER_POWER_W=1800',
                'WATER_HEATER_DURATION_MINUTES=60',
                'WATER_HEATER_ENERGY_KWH=',
                'COOKER_ENABLED=true',
                'COOKER_TIME="18:30"',
                'COOKER_POWER_W=2500',
                'COOKER_DURATION_MINUTES=30',
                'CONTROL_MAX_SUCCESSFUL_WRITES_PER_DAY=4',
                'CONTROL_MAX_SUCCESSFUL_WRITES_WITH_BONUS=5',
                'CONTROL_MAX_ORDER_SUBMISSIONS_PER_DAY=8',
            ]) + "\n")
            with mock.patch.dict(os.environ, {}, clear=False):
                cfg = load_config(str(p))
            self.assertEqual(cfg["grid"]["export_hard_limit_w"], 900)
            self.assertEqual(cfg["pv"]["arrays"][0]["tilt_deg"], 35.0)
            self.assertEqual(cfg["water_heater"]["scheduled_time"], "09:15")
            self.assertAlmostEqual(cfg["water_heater"]["scheduled_energy_kwh"], 1.8)
            self.assertTrue(cfg["cooker"]["enabled"])
            self.assertEqual(cfg["control"]["max_successful_writes_with_bonus"], 5)
            redacted = redact_env_lines(p)
            self.assertNotIn('pw#1', redacted)
            self.assertNotIn('mail@example.com', redacted)

    def test_process_environment_overrides_file(self):
        arrays = [{"name":"roof","kwp":1,"tilt_deg":0,"azimuth_deg":0}]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "deye.env"
            p.write_text("\n".join([
                'DEYE_APP_ID="id"','DEYE_APP_SECRET="s"','DEYE_LOGIN="l"','DEYE_PASSWORD="p"','DEYE_INVERTER_SN="1"',
                'GRID_EXPORT_HARD_LIMIT_W=800',
                'PV_ARRAYS_JSON=' + json.dumps(json.dumps(arrays)),
            ]) + "\n")
            with mock.patch.dict(os.environ, {"GRID_EXPORT_HARD_LIMIT_W":"1000"}):
                cfg = load_config(str(p))
            self.assertEqual(cfg["grid"]["export_hard_limit_w"], 1000)


if __name__ == "__main__":
    unittest.main()
