import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import discover


class FakeClient:
    def __init__(self, payload, *a, **kw):
        self.payload = payload
    def get_token(self):
        return "tok"
    def list_stations_with_devices(self, page=1, size=50):
        return self.payload


def run(payload, argv):
    """Run the CLI against a canned API payload and capture what it printed."""
    buf = io.StringIO()
    with mock.patch.object(sys, "argv", ["deyeopt-discover"] + argv), \
         mock.patch("deye_api.DeyeClient", lambda *a, **kw: FakeClient(payload)), \
         redirect_stdout(buf):
        code = discover.main()
    return code, buf.getvalue()


CREDS = ["--app-id", "a", "--app-secret", "b", "--login", "c@d.e", "--password", "f",
         "--env", "/nonexistent.env"]


def station(sid, name, items, kwp=10.0):
    return {"id": sid, "name": name, "installedCapacity": kwp,
            "regionTimezone": "Europe/Amsterdam", "deviceListItems": items}


def dev(sn, kind):
    return {"deviceSn": sn, "deviceType": kind}


class DiscoverTests(unittest.TestCase):
    def test_reports_the_inverter_and_not_the_logger(self):
        payload = {"stationList": [station(11, "Home", [
            dev("LOGGER111", "COLLECTOR"), dev("INV222", "INVERTER")])]}
        code, out = run(payload, CREDS)
        self.assertEqual(code, 0)
        self.assertIn('DEYE_INVERTER_SN="INV222"', out)
        self.assertIn("DEYE_STATION_ID=11", out)
        # The logger serial must never be offered as the value to configure.
        self.assertNotIn('DEYE_INVERTER_SN="LOGGER111"', out)

    def test_hides_other_devices_unless_asked(self):
        payload = {"stationList": [station(11, "Home", [
            dev("LOGGER111", "COLLECTOR"), dev("INV222", "INVERTER")])]}
        _, quiet = run(payload, CREDS)
        self.assertNotIn("LOGGER111", quiet)
        _, verbose = run(payload, CREDS + ["--all"])
        self.assertIn("LOGGER111", verbose)
        self.assertIn("logger", verbose)

    def test_several_inverters_are_listed_rather_than_guessed(self):
        payload = {"stationList": [
            station(11, "A", [dev("INV1", "INVERTER")]),
            station(22, "B", [dev("INV2", "INVERTER")])]}
        code, out = run(payload, CREDS)
        self.assertEqual(code, 0)
        self.assertIn("INV1", out)
        self.assertIn("INV2", out)
        self.assertIn("Pick the one", out)

    def test_microinverter_only_account_is_reported_as_unsupported(self):
        payload = {"stationList": [station(11, "Balcony", [
            dev("LOG", "COLLECTOR"), dev("MI1", "MICRO_INVERTER"),
            dev("MI1-1", "PV_MODULE")], kwp=0.9)]}
        code, out = run(payload, CREDS)
        self.assertEqual(code, 5)
        self.assertIn("No INVERTER-type device", out)

    def test_account_with_no_plants(self):
        code, out = run({"stationList": []}, CREDS)
        self.assertEqual(code, 4)
        self.assertIn("no plants", out)

    def test_credentials_are_read_from_an_env_file(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / "d.env"
            env.write_text('DEYE_APP_ID="x"\nDEYE_APP_SECRET="y"\n'
                           'DEYE_LOGIN="z@q.e"\nDEYE_PASSWORD="w"\n')
            payload = {"stationList": [station(11, "Home", [dev("INV9", "INVERTER")])]}
            code, out = run(payload, ["--env", str(env)])
        self.assertEqual(code, 0)
        self.assertIn('DEYE_INVERTER_SN="INV9"', out)
