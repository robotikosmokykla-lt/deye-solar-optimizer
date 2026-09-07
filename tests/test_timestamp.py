import datetime as dt
import unittest
from zoneinfo import ZoneInfo
from deye_api import parse_deye_timestamp

class TimestampTests(unittest.TestCase):
    def test_epoch(self):
        tz=ZoneInfo('Europe/Amsterdam')
        self.assertIsNotNone(parse_deye_timestamp(1756742400,tz))
    def test_iso(self):
        tz=ZoneInfo('Europe/Amsterdam')
        p=parse_deye_timestamp('2026-09-01T22:05:00+02:00',tz)
        self.assertEqual(p.hour,22)
    def test_plain(self):
        tz=ZoneInfo('Europe/Amsterdam')
        p=parse_deye_timestamp('2026-09-01 22:05:00',tz)
        self.assertEqual(p.hour,22)

if __name__ == '__main__': unittest.main()
