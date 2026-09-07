import unittest
from unittest.mock import patch
from solar_forecast import PVArray, fetch_forecast


def fake_response(azimuth):
    # One synthetic day, four 15-minute points around sunrise.
    base = {
        'minutely_15': {
            'time': ['2026-09-02T06:15','2026-09-02T06:30','2026-09-02T06:45','2026-09-02T07:00'],
            'global_tilted_irradiance': [0,10,100,200],
        },
        'daily': {
            'time': ['2026-09-02'],
            'sunrise': ['2026-09-02T06:30'],
            'sunset': ['2026-09-02T20:10'],
        },
    }
    return base


class ForecastTests(unittest.TestCase):
    @patch('solar_forecast._request_array')
    def test_multi_array_wakeup_and_energy(self, mock_req):
        mock_req.side_effect = lambda lat,lon,tz,array,days: fake_response(array.azimuth_deg)
        fc = fetch_forecast(
            52.0,13.0,'Europe/Berlin',
            [PVArray('east',5.0,35,-90),PVArray('west',5.0,35,90),PVArray('north',1.0,90,180)],
            0.82,30,250,15,2,
        )
        day = fc[next(iter(fc))]
        # At 06:30 the combined arrays exceed 30 W; +15 min bias => 06:45.
        self.assertEqual(day.pv_wakeup.strftime('%H:%M'),'06:45')
        self.assertGreater(day.expected_kwh,0)
        self.assertEqual(set(day.array_kwh),{'east','west','north'})

if __name__ == '__main__': unittest.main()
