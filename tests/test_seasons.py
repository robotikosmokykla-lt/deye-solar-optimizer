import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from config_loader import load_config
from seasonal_policy import seasonal_defaults, reserve_request
from season_manager import apply_native, main, fingerprint


def settings():
    return {
        'touAction': 'on', 'gridChargeAction': 'on', 'gridChargeAmpere': 20,
        'workMode': 'SELLING_FIRST', 'maxSellPower': 0,
        'timeUseSettingItems': [
            dict(time=t, soc=18 if t < '07:00' or t >= '17:00' else 15,
                 power=12000, voltage=49, enableGridCharge=True,
                 enableGeneration=False, enableSell=False)
            for t in ['01:00', '07:00', '09:00', '13:00', '17:00', '21:00']
        ],
    }


def config():
    return {'season': {'mode': 'auto', 'active': 'winter', 'grid_charge_ampere': 20},
            'battery': {'soc_floor_pct': 15},
            'night_strategy': {'morning_soc_pct': 20, 'max_export_w': 0},
            'deye': {'inverter_sn': 'TEST'}, 'control': {'dry_run': False}}


class SeasonTests(unittest.TestCase):
    def test_calendar_uses_site_timezone_at_season_boundaries(self):
        values = {'SEASON_MODE': 'auto', 'SITE_TIMEZONE': 'Europe/Vilnius'}
        utc = dt.timezone.utc
        for now, expected in [(dt.datetime(2026, 3, 31, 20, 59, tzinfo=utc), 'winter'),
                              (dt.datetime(2026, 3, 31, 21, 0, tzinfo=utc), 'summer'),
                              (dt.datetime(2026, 9, 30, 21, 0, tzinfo=utc), 'winter')]:
            season, defaults = seasonal_defaults(values, now=now)
            self.assertEqual(season['active'], expected)
            self.assertEqual(defaults['NIGHT_MORNING_SOC_PCT'], '20' if expected == 'winter' else '18')

    def test_disabled_pinned_and_invalid_modes(self):
        self.assertEqual(seasonal_defaults({})[1], {})
        self.assertEqual(seasonal_defaults({'SEASON_MODE': 'winter'})[0]['active'], 'winter')
        for values in [{'SEASON_MODE': 'bad'}, {'SEASON_WINTER_MONTHS': '13'},
                       {'SEASON_GRID_CHARGE_AMPERE': '0'}]:
            with self.assertRaises(ValueError):seasonal_defaults(values)

    def test_explicit_file_and_process_overrides_win(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            p = Path(td) / 'deye.env'
            p.write_text('\n'.join([
                'DEYE_APP_ID=id', 'DEYE_APP_SECRET=s', 'DEYE_LOGIN=l',
                'DEYE_PASSWORD=p', 'DEYE_INVERTER_SN=TEST', 'SEASON_MODE=winter',
                'PV_ARRAYS_JSON=[{"kwp":1,"tilt_deg":0,"azimuth_deg":0}]',
                'NIGHT_MORNING_SOC_PCT=',
            ]))
            self.assertEqual(load_config(str(p))['night_strategy']['morning_soc_pct'], 20)
            with patch.dict(os.environ, {'NIGHT_MORNING_SOC_PCT': '25'}):
                self.assertEqual(load_config(str(p))['night_strategy']['morning_soc_pct'], 25)
            p.write_text(p.read_text() + '\nNIGHT_MORNING_SOC_PCT=22\n')
            self.assertEqual(load_config(str(p))['night_strategy']['morning_soc_pct'], 22)

    def test_native_request_preserves_unrelated_settings(self):
        before = settings()
        original = copy.deepcopy(before)
        desired = reserve_request(config(), before)
        self.assertEqual(before, original)
        self.assertEqual(set(desired), {'gridChargeAction', 'gridChargeAmpere', 'timeUseSettingItems'})
        for a, b in zip(before['timeUseSettingItems'], desired['timeUseSettingItems']):
            self.assertEqual({k:v for k,v in a.items() if k != 'soc'},
                             {k:v for k,v in b.items() if k != 'soc'})
            self.assertEqual(b['soc'], 15 if '07:00' <= b['time'] < '17:00' else 20)

    def test_custom_boundaries_and_disabled_tou_are_refused(self):
        for before in [dict(settings(), touAction='off'), settings()]:
            before['timeUseSettingItems'][1]['time'] = '05:00'
            with self.assertRaises(ValueError):reserve_request(config(), before)

    @patch('season_manager.write_audit')
    @patch('season_manager.wait_terminal', return_value={'status': 666})
    @patch('season_manager.read_settings')
    def test_apply_requires_terminal_success_and_matching_readback(self, read, wait, audit):
        cfg = config(); before = settings(); after = {**before, **reserve_request(cfg, before)}
        read.side_effect = [before, after]
        client = Mock(); client.set_reserve_schedule.return_value = {'orderId': 123}
        state = {}; saves = []
        apply_native(client, cfg, state, lambda s: saves.append(copy.deepcopy(s)))
        self.assertEqual(client.set_reserve_schedule.call_count, 1)
        self.assertTrue(any(s.get('pending_order_id') == 123 for s in saves))
        self.assertEqual(state['applied'], fingerprint(cfg))
        self.assertNotIn('retry_after', state)

    @patch('season_manager.write_audit')
    @patch('season_manager.wait_terminal', return_value={'status': 666})
    @patch('season_manager.read_settings', side_effect=lambda *a: settings())
    def test_mismatched_readback_does_not_mark_applied(self, read, wait, audit):
        client = Mock(); client.set_reserve_schedule.return_value = {'orderId': 123}
        state = {}
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            apply_native(client, config(), state, lambda s: None)
        self.assertNotIn('applied', state)
        self.assertIn('retry_after', state)

    @patch('season_manager.read_settings', side_effect=lambda *a: settings())
    def test_timeout_is_guarded_before_submission_and_not_retried(self, read):
        client = Mock(); client.set_reserve_schedule.side_effect = TimeoutError('network')
        state = {}
        with self.assertRaises(TimeoutError):apply_native(client, config(), state, lambda s: None)
        with self.assertRaisesRegex(RuntimeError, 'backoff'):
            apply_native(client, config(), state, lambda s: None)
        self.assertEqual(client.set_reserve_schedule.call_count, 1)

    @patch('season_manager.wait_terminal', side_effect=RuntimeError('still pending'))
    def test_pending_order_survives_restart_without_duplicate_submission(self, wait):
        client = Mock(); state = {'pending_order_id': 123}
        with self.assertRaisesRegex(RuntimeError, 'pending'):
            apply_native(client, config(), state, lambda s: None)
        client.set_reserve_schedule.assert_not_called()
        self.assertEqual(state['pending_order_id'], 123)

    @patch('season_manager.make_deye_client')
    @patch('season_manager.service')
    @patch('season_manager.load_config')
    def test_dry_run_never_calls_api_or_service(self, load, service, client):
        cfg = config(); cfg['control']['dry_run'] = True; load.return_value = cfg
        with patch('sys.argv', ['season_manager', '--apply']):self.assertEqual(main(), 0)
        client.assert_not_called(); service.assert_not_called()

    @patch('season_manager.make_deye_client')
    @patch('season_manager.service')
    @patch('season_manager.load_config')
    def test_confirmed_profile_does_not_touch_manual_edits(self, load, service, client):
        with tempfile.TemporaryDirectory() as td:
            cfg = config(); cfg['logging'] = {'state_db': str(Path(td) / 'state.db')}; load.return_value = cfg
            (Path(td) / 'season-state.json').write_text(json.dumps({'applied': fingerprint(cfg)}))
            with patch('sys.argv', ['season_manager', '--apply']):self.assertEqual(main(), 0)
            client.assert_not_called(); service.assert_not_called()

    @patch('season_manager.make_deye_client')
    @patch('season_manager.check_control_idle')
    @patch('season_manager.apply_native', side_effect=RuntimeError('offline'))
    @patch('season_manager.service_active', return_value=True)
    @patch('season_manager.service')
    @patch('season_manager.load_config')
    def test_controller_restarts_if_season_update_fails(self, load, service, active, apply, idle, client):
        with tempfile.TemporaryDirectory() as td:
            cfg = config(); cfg['logging'] = {'state_db': str(Path(td) / 'state.db')}; load.return_value = cfg
            with patch('sys.argv', ['season_manager', '--apply']):
                with self.assertRaisesRegex(RuntimeError, 'offline'):main()
            self.assertEqual([c.args[0] for c in service.call_args_list], ['stop', 'start'])
