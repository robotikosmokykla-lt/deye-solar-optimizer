import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deye_api import DeyeClient, flatten_device_latest, is_busy_error, is_offline_error, DeyeAPIError


def make_creds(td):
    p = Path(td)
    (p / "app-id.txt").write_text("id")
    (p / "app-secret.txt").write_text("secret")
    (p / "login.txt").write_text("x@example.com")
    (p / "login-pass.txt").write_text("pass")


class ApiTests(unittest.TestCase):
    def test_dynamic_control_only_sends_requested_field(self):
        with tempfile.TemporaryDirectory() as td:
            make_creds(td)
            c = DeyeClient("https://example.invalid/v1.0", td)
            with patch.object(c, "_request", return_value={"success": True, "connectionStatus": 1, "orderId": 10}) as req:
                c.dynamic_control("2512", max_sell_power=400)
                body = req.call_args.args[1]
                self.assertEqual(body, {"deviceSn": "2512", "maxSellPower": 400})

    def test_dynamic_control_can_atomically_carry_work_mode_and_hard_cap(self):
        with tempfile.TemporaryDirectory() as td:
            make_creds(td)
            c = DeyeClient("https://example.invalid/v1.0", td)
            with patch.object(c, "_request", return_value={"success": True, "connectionStatus": 1, "orderId": 12}) as req:
                c.dynamic_control("2512", max_sell_power=1000, work_mode="SELLING_FIRST")
                body = req.call_args.args[1]
                self.assertEqual(
                    body,
                    {"deviceSn": "2512", "maxSellPower": 1000, "workMode": "SELLING_FIRST"},
                )

    def test_dynamic_control_rejects_unknown_work_mode(self):
        with tempfile.TemporaryDirectory() as td:
            make_creds(td)
            c = DeyeClient("https://example.invalid/v1.0", td)
            with self.assertRaises(ValueError):
                c.dynamic_control("2512", max_sell_power=1000, work_mode="BAD_MODE")

    def test_profile_endpoints_use_narrow_dedicated_paths(self):
        with tempfile.TemporaryDirectory() as td:
            make_creds(td)
            c = DeyeClient("https://example.invalid/v1.0", td)
            with patch.object(c, "_request", return_value={"success": True, "connectionStatus": 1, "orderId": 10}) as req:
                c.set_work_mode("2512", "SELLING_FIRST")
                self.assertTrue(req.call_args.args[0].endswith("/order/sys/workMode/update"))
                self.assertEqual(req.call_args.args[1], {"deviceSn": "2512", "workMode": "SELLING_FIRST"})
            with patch.object(c, "_request", return_value={"success": True, "connectionStatus": 1, "orderId": 11}) as req:
                c.set_energy_pattern("2512", "LOAD_FIRST")
                self.assertTrue(req.call_args.args[0].endswith("/order/sys/energyPattern/update"))
                self.assertEqual(req.call_args.args[1], {"deviceSn": "2512", "energyPattern": "LOAD_FIRST"})
            with patch.object(c, "_request", return_value={"success": True, "maxChargeCurrent": 180}) as req:
                c.get_battery_config("2512")
                self.assertTrue(req.call_args.args[0].endswith("/config/battery"))


    def test_flatten_device_latest(self):
        flat = flatten_device_latest({
            "deviceDataList": [{
                "collectionTime": 1788300677,
                "deviceState": 3,
                "dataList": [
                    {"key": "SOC", "value": "32"},
                    {"key": "BatteryPower", "value": "1209"},
                ],
            }]
        })
        self.assertEqual(flat["metrics"]["SOC"], "32")
        self.assertEqual(flat["metrics"]["BatteryPower"], "1209")

    def test_offline_error_detection(self):
        self.assertTrue(is_offline_error(DeyeAPIError("write failed: 2104006 device offline", 2104006)))
        self.assertTrue(is_offline_error(DeyeAPIError("config failed: 2106002 data upload failed", 2106002)))

    def test_busy_error_detection(self):
        self.assertTrue(is_busy_error(DeyeAPIError("write failed: 2104004 command concurrent running", 2104004)))
        self.assertFalse(is_busy_error(DeyeAPIError("write failed: 2104006 device offline", 2104006)))


if __name__ == "__main__":
    unittest.main()
