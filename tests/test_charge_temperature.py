import copy
import datetime as dt
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from charge_temperature import ChargeTemperatureGuard, current_limit_a, KEY, cold_maintenance_limit, blocks_command_queue
from deye_api import DeyeAPIError


class MemoryDB:
    def __init__(self, state):self.values = {KEY: copy.deepcopy(state)}; self.events = []
    def get(self, key, default=None):return copy.deepcopy(self.values.get(key, default))
    def set(self, key, value, now):self.values[key] = copy.deepcopy(value)
    def add_api_event(self, *args):self.events.append(args)


class TemperatureTests(unittest.TestCase):
    now = dt.datetime(2026, 10, 26, 22, tzinfo=dt.timezone.utc)

    def make_guard(self, current=150, **options):
        cfg = {'charge_temperature': {'enabled': True, 'capacity_ah': 300,
               'max_current_a': 160, 'max_age_minutes': 10, 'hysteresis_c': 2,
               'recovery_minutes': 15, **options},
               'deye': {'inverter_sn': 'TEST'}, 'control': {'dry_run': False}}
        db = MemoryDB({'confirmed_a': current, 'day': self.now.date().isoformat(), 'raises_today': 0})
        client = Mock()
        client.set_battery_parameter.return_value = {'orderId': 123}
        return ChargeTemperatureGuard(cfg, db, client, Mock())

    def snap(self, temp, now=None, age=0):
        return SimpleNamespace(collection_at=(now or self.now) - dt.timedelta(minutes=age),
                               metrics={'Temperature- Battery': temp})

    def test_victron_chart_boundaries_and_whole_amp_rounding(self):
        for temp, expected in [(4.99, 0), (5, 60), (9.99, 60), (10, 90),
                               (14.99, 90), (15, 150), (34.99, 150),
                               (35, 90), (49.99, 90), (50, 0), (-10, 0)]:
            self.assertEqual(current_limit_a(temp, 300, 160), expected)
        self.assertEqual(current_limit_a(12, 15000 / 51.2, 160), 87)
        self.assertEqual(current_limit_a(20, 300, 50), 50)

    def test_reduction_is_immediate_even_if_raise_budget_is_exhausted(self):
        g = self.make_guard(); g.state['raises_today'] = 4
        self.assertTrue(g.tick(self.now, self.snap(4)))
        g.client.set_battery_parameter.assert_called_once_with('TEST', 'MAX_CHARGE_CURRENT', 0)

    def test_zero_limit_really_sends_zero_not_a_minimum_current(self):
        g = self.make_guard(current=60)
        g.tick(self.now, self.snap(0))
        self.assertEqual(g.state['pending']['target_a'], 0)

    def test_missing_stale_invalid_and_future_temperature_stop_charging(self):
        for snap in [None, self.snap(None), self.snap('nan'), self.snap('inf'),
                     self.snap('bad'), self.snap(150), self.snap(20, age=10.1),
                     self.snap(20, age=-2)]:
            g = self.make_guard(); g.tick(self.now, snap)
            self.assertEqual(g.client.set_battery_parameter.call_args.args[-1], 0)

    def test_recovery_waits_for_margin_and_stability(self):
        g = self.make_guard(current=90)
        g.tick(self.now, self.snap(16.9)); g.client.set_battery_parameter.assert_not_called()
        g.tick(self.now, self.snap(17))
        t = self.now + dt.timedelta(minutes=14)
        g.tick(t, self.snap(17, t)); g.client.set_battery_parameter.assert_not_called()
        t += dt.timedelta(minutes=1)
        g.tick(t, self.snap(17, t))
        g.client.set_battery_parameter.assert_called_once_with('TEST', 'MAX_CHARGE_CURRENT', 150)

    def test_cold_recovery_requires_seven_degrees(self):
        g = self.make_guard(current=0)
        g.tick(self.now, self.snap(6.9)); self.assertNotIn('recovery', g.state)
        g.tick(self.now, self.snap(7)); self.assertIn('recovery', g.state)

    def test_hot_recovery_requires_cooling_margin(self):
        g = self.make_guard(current=0)
        g.tick(self.now, self.snap(49)); self.assertNotIn('recovery', g.state)
        g.tick(self.now, self.snap(48)); self.assertIn('recovery', g.state)

    def test_stale_reading_resets_recovery(self):
        g = self.make_guard(current=90)
        g.tick(self.now, self.snap(20)); self.assertIn('recovery', g.state)
        t = self.now + dt.timedelta(minutes=11)
        g.tick(t, self.snap(20, self.now))
        self.assertNotIn('recovery', g.state)
        self.assertEqual(g.state['pending']['target_a'], 0)

    def test_dry_run_never_changes_hardware_or_claims_confirmation(self):
        g = self.make_guard(); g.cfg['control']['dry_run'] = True
        self.assertFalse(g.tick(self.now, self.snap(4)))
        g.client.set_battery_parameter.assert_not_called()
        self.assertEqual(g.state['confirmed_a'], 150)

    def test_export_order_has_to_finish_before_temperature_submission(self):
        g = self.make_guard()
        self.assertTrue(g.tick(self.now, self.snap(4), export_pending=True))
        g.client.set_battery_parameter.assert_not_called()

    def test_accepted_order_survives_restart_and_is_not_duplicated(self):
        g = self.make_guard(); g.tick(self.now, self.snap(4))
        next_guard = ChargeTemperatureGuard(g.cfg, g.db, g.client, g.log)
        g.client.check_order_status.return_value = {'status': 100}
        next_guard.tick(self.now, self.snap(4))
        self.assertEqual(g.client.set_battery_parameter.call_count, 1)
        self.assertEqual(next_guard.state['pending']['id'], 123)

    def test_success_requires_an_independent_readback(self):
        g = self.make_guard(); g.tick(self.now, self.snap(4))
        g.client.check_order_status.return_value = {'status': 666}
        g.tick(self.now, self.snap(4))
        self.assertEqual(g.state['confirmed_a'], 150)
        t = self.now + dt.timedelta(seconds=15)
        g.client.dynamic_control_read.return_value = {'orderId': 456}
        g.tick(t, self.snap(4, t))
        self.assertEqual(g.state['pending']['kind'], 'read')
        g.client.dynamic_control_read_result.return_value = {'maxChargeCurrent': 0}
        t += dt.timedelta(seconds=15); g.tick(t, self.snap(4, t))
        self.assertEqual(g.state['confirmed_a'], 0)
        self.assertNotIn('pending', g.state)

    def test_failed_order_does_not_trust_the_old_current_limit(self):
        g = self.make_guard(); g.tick(self.now, self.snap(4))
        g.client.check_order_status.return_value = {'status': 500, 'error': '540'}
        g.tick(self.now, self.snap(4))
        self.assertIsNone(g.state['confirmed_a'])
        self.assertNotIn('pending', g.state)
        g.tick(self.now, self.snap(4))
        self.assertEqual(g.client.set_battery_parameter.call_count, 1)

    def test_readback_mismatch_does_not_claim_a_safe_limit(self):
        g = self.make_guard(); g.state['pending'] = {'kind': 'read', 'id': 456, 'target_a': 0}
        g.client.dynamic_control_read_result.return_value = {'maxChargeCurrent': 160}
        g.tick(self.now, self.snap(4)); self.assertIsNone(g.state['confirmed_a'])
        self.assertIn('mismatch', g.state['last_error'])

    def test_ambiguous_submission_is_guarded_across_restart(self):
        g = self.make_guard(); g.client.set_battery_parameter.side_effect = TimeoutError('network')
        g.tick(self.now, self.snap(4))
        next_guard = ChargeTemperatureGuard(g.cfg, g.db, g.client, g.log)
        next_guard.tick(self.now + dt.timedelta(minutes=1), self.snap(4))
        self.assertEqual(g.client.set_battery_parameter.call_count, 1)
        self.assertIsNone(next_guard.state['confirmed_a'])

    def test_explicit_offline_rejection_retries_after_a_minute(self):
        g = self.make_guard(); g.client.set_battery_parameter.side_effect = DeyeAPIError('offline', 2104006)
        g.tick(self.now, self.snap(4))
        g.tick(self.now + dt.timedelta(seconds=59), self.snap(4))
        self.assertEqual(g.client.set_battery_parameter.call_count, 1)
        g.tick(self.now + dt.timedelta(seconds=60), self.snap(4))
        self.assertEqual(g.client.set_battery_parameter.call_count, 2)

    def test_disabling_guard_still_drains_an_existing_order(self):
        g = self.make_guard(); g.tick(self.now, self.snap(4)); g.options['enabled'] = False
        g.client.check_order_status.return_value = {'status': 100}
        self.assertTrue(g.tick(self.now, self.snap(4)))
        g.client.check_order_status.assert_called_once()

    def maintenance_guard(self, current=90):
        return self.make_guard(current=current, cold_maintenance_enabled=True,
                               cold_maintenance_min_c=2, cold_maintenance_current_a=5)

    def storage_snap(self, temperature, soc=40, now=None, age=0):
        snap = self.snap(temperature, now, age)
        snap.metrics['SOC'] = soc
        return snap

    def test_custom_maintenance_reduces_to_five_amps_above_freezing(self):
        g=self.maintenance_guard()
        g.tick(self.now,self.storage_snap(4))
        g.client.set_battery_parameter.assert_called_once_with('TEST','MAX_CHARGE_CURRENT',5)
        self.assertEqual(g.state['reason'],'cold_maintenance_replenishment')

    def test_custom_maintenance_never_overrides_freezing_or_stale_temperature(self):
        for snap in [self.storage_snap(-5),self.storage_snap(0),self.storage_snap(1.99),
                     self.storage_snap(4,age=10.1),self.storage_snap('nan')]:
            g=self.maintenance_guard();g.tick(self.now,snap)
            self.assertEqual(g.client.set_battery_parameter.call_args.args[-1],0)

    def test_custom_maintenance_stops_at_storage_target_and_holds_until_fifty_five(self):
        g=self.maintenance_guard(current=5)
        state={}
        for soc, expected in [(59,5),(60,0),(58,0),(55,5)]:
            value,reason=cold_maintenance_limit(4,soc,g.options,{},state)
            self.assertEqual(value,expected)
        restarted=copy.deepcopy(state)
        value,_=cold_maintenance_limit(4,60,g.options,{},restarted)
        value,_=cold_maintenance_limit(4,58,g.options,{},restarted)
        self.assertEqual(value,0)

    def test_custom_maintenance_requires_valid_soc_and_never_exceeds_sixty(self):
        for soc in [None,float('nan'),-1,101]:
            g=self.maintenance_guard();g.tick(self.now,self.storage_snap(4,soc))
            self.assertEqual(g.client.set_battery_parameter.call_args.args[-1],0)
        g=self.maintenance_guard()
        limit,_=cold_maintenance_limit(4,61,g.options,{'storage_soc_pct':70},{})
        self.assertEqual(limit,0)

    def test_custom_maintenance_recovery_needs_two_point_five_and_stability(self):
        g=self.maintenance_guard(current=0)
        g.tick(self.now,self.storage_snap(2.4));self.assertNotIn('recovery',g.state)
        g.tick(self.now,self.storage_snap(3))
        t=self.now+dt.timedelta(minutes=15)
        g.tick(t,self.storage_snap(3,now=t))
        g.client.set_battery_parameter.assert_called_once_with('TEST','MAX_CHARGE_CURRENT',5)

    def test_custom_maintenance_stays_slow_until_normal_cold_recovery(self):
        g=self.maintenance_guard(current=5);g.state['cold_maintenance_active']=True
        g.tick(self.now,self.storage_snap(6,60))
        g.client.set_battery_parameter.assert_called_once_with('TEST','MAX_CHARGE_CURRENT',0)
        self.assertTrue(g.state['cold_maintenance_hold'])
        state={'cold_maintenance_active':True,'cold_maintenance_hold':True}
        value,_=cold_maintenance_limit(7,60,g.options,{},state)
        self.assertIsNone(value)
        self.assertFalse(state['cold_maintenance_active'])

    def test_custom_maintenance_obeys_small_bank_c_rate_limit(self):
        g=self.maintenance_guard();g.options['capacity_ah']=100
        g.tick(self.now,self.storage_snap(4))
        self.assertEqual(g.client.set_battery_parameter.call_args.args[-1],2)

    def test_custom_replenishment_is_not_starved_by_normal_raise_budget(self):
        g=self.maintenance_guard(current=0);g.state['raises_today']=4
        g.tick(self.now,self.storage_snap(4,55))
        t=self.now+dt.timedelta(minutes=15)
        g.tick(t,self.storage_snap(4,55,now=t))
        g.client.set_battery_parameter.assert_called_once_with('TEST','MAX_CHARGE_CURRENT',5)

    def test_critical_ten_percent_never_allows_subzero_charging(self):
        for temperature in [-20,0,1.9]:
            g=self.maintenance_guard()
            g.tick(self.now,self.storage_snap(temperature,10))
            self.assertTrue(g.state['critical_low_soc'])
            self.assertIn('critical_low_soc',g.state['alerts'])
            g.client.set_battery_parameter.assert_called_once_with('TEST','MAX_CHARGE_CURRENT',0)

    def test_critical_alarm_is_latched_until_fifteen_and_stale_soc_does_not_clear_it(self):
        g=self.make_guard(current=0)
        g.tick(self.now,self.storage_snap(-20,10))
        self.assertTrue(g.state['critical_low_soc'])
        g.tick(self.now,self.storage_snap(-20,14))
        self.assertTrue(g.state['critical_low_soc'])
        g.tick(self.now,self.storage_snap(-20,80,age=15))
        self.assertTrue(g.state['critical_low_soc'])
        g.tick(self.now,self.storage_snap(-20,15))
        self.assertFalse(g.state['critical_low_soc'])

    def test_critical_alarm_does_not_wait_for_pending_hardware_order(self):
        g=self.maintenance_guard()
        g.state['pending']={'kind':'write','id':123,'target_a':0}
        g.client.check_order_status.return_value={'status':100}
        g.tick(self.now,self.storage_snap(-20,10))
        self.assertTrue(g.state['critical_low_soc'])
        g.client.set_battery_parameter.assert_not_called()

    def test_rejected_charge_order_backoff_does_not_freeze_export_queue(self):
        g=self.make_guard();g.tick(self.now,self.snap(4))
        g.client.check_order_status.return_value={'status':500,'error':'540'}
        g.tick(self.now,self.snap(4))
        self.assertFalse(g.tick(self.now,self.snap(4)))
        self.assertFalse(blocks_command_queue(g.state,self.now))
        self.assertEqual(g.client.set_battery_parameter.call_count,1)

    def test_uncertain_charge_submission_still_blocks_export_queue(self):
        g=self.make_guard();g.client.set_battery_parameter.side_effect=TimeoutError('network')
        self.assertTrue(g.tick(self.now,self.snap(4)))
        self.assertTrue(g.tick(self.now,self.snap(4)))
        self.assertTrue(blocks_command_queue(g.state,self.now))

    def test_legacy_confirmed_rejection_is_distinct_from_unknown_submit(self):
        until=(self.now+dt.timedelta(hours=1)).isoformat()
        self.assertFalse(blocks_command_queue({'retry_after':until,'last_error':'write rejected: 540'},self.now))
        self.assertTrue(blocks_command_queue({'retry_after':until,'last_error':'network timeout'},self.now))
