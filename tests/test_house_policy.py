import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from house_policy import apply_plan, build_plan, reserve_for_temperature
from seasonal_policy import reserve_request
from charge_temperature import ChargeTemperatureGuard
from controller import Controller, Config
from config_loader import load_config
from mode_manager import update_mode


def cfg(mode='occupied'):
    return {'house': {'enabled': True, 'mode': mode, 'storage_soc_pct': 60,
                     'base_day_soc': 15, 'base_night_soc': 20, 'weather_station': ''},
            'battery': {'soc_floor_pct': 15, 'effective_kwh': 15},
            'night_strategy': {'morning_soc_pct': 20, 'max_export_w': 0},
            'season': {'grid_charge_ampere': 20}, 'grid': {'day_export_w': 1000},
            'control': {'dry_run': False}, 'deye': {'inverter_sn': 'TEST'},
            'charge_temperature': {'enabled': True, 'capacity_ah': 300,
                 'max_current_a': 160, 'max_age_minutes': 10, 'hysteresis_c': 2, 'recovery_minutes': 15}}


class HousePolicyTests(unittest.TestCase):
    now = dt.datetime(2026, 10, 27, 0, tzinfo=dt.timezone.utc)

    def weather(self, current=10, minimum=10):
        return {'fetched_at': self.now.isoformat(), 'source': 'test', 'current_air_c': current,
                'points': [((self.now+dt.timedelta(hours=h)).isoformat(), minimum) for h in range(49)]}

    def test_reserve_table_boundaries(self):
        for temp, reserve in [(15,20),(14,30),(12,30),(11,40),(10,40),(9,50),(7,50),(6,60),(-5,60)]:
            self.assertEqual(reserve_for_temperature(temp), reserve)

    def test_occupied_prepares_before_measured_battery_is_cold(self):
        plan = build_plan(cfg(), {}, [(self.now,15,100)], self.now, self.weather(current=12,minimum=2))
        self.assertEqual(plan['reserve_soc_pct'], 60)
        self.assertEqual(plan['estimated_min_battery_c'], 5)
        self.assertEqual(plan['grid_charge_a'], 10)

    def test_warm_forecast_preserves_normal_morning_window(self):
        c=cfg();p=build_plan(c,{},[(self.now,16,100)],self.now,self.weather())
        apply_plan(c,p)
        self.assertEqual(c['battery']['soc_floor_pct'],15)
        self.assertEqual(c['night_strategy']['morning_soc_pct'],20)

    def test_cold_reserve_applies_to_all_hours(self):
        c=cfg();apply_plan(c,{'mode':'occupied','reserve_soc_pct':50,'grid_charge_a':10})
        self.assertEqual(c['battery']['soc_floor_pct'],50)
        self.assertEqual(c['night_strategy']['morning_soc_pct'],50)

    def test_cold_reserve_tracks_derating_and_reaches_every_native_slot(self):
        # Measured cold alone must raise reserves even without weather forecasts.
        from charge_temperature import current_limit_a
        current={'touAction':'on','timeUseSettingItems':[{'time':t,'soc':15,'power':12000,'voltage':49,
                 'enableGridCharge':True,'enableGeneration':False,'enableSell':False}
                 for t in ['01:00','07:00','09:00','13:00','17:00','21:00']]}
        for temperature, reserve in [(14,30),(11,40),(8,50),(6,60),(4.9,60),(0,60),(-5,60)]:
            c=cfg()
            plan=build_plan(c,{},[(self.now,temperature,100)],self.now)
            self.assertEqual(plan['reserve_soc_pct'],reserve)
            apply_plan(c,plan)
            request=reserve_request(c,current)
            self.assertTrue(all(item['soc']==reserve for item in request['timeUseSettingItems']))
            if temperature < 5:
                self.assertEqual(current_limit_a(temperature,300,160),0)
                self.assertIn('charging_blocked_cold',plan['alerts'])

    def test_below_five_degrees_stops_charging_even_with_low_soc_and_high_reserve(self):
        from types import SimpleNamespace
        g=self.guard(mode='occupied')
        plan=build_plan(g.cfg,{},[(self.now,4,100)],self.now)
        apply_plan(g.cfg,plan)
        self.assertEqual(g.cfg['battery']['soc_floor_pct'],60)
        sample=SimpleNamespace(collection_at=self.now,metrics={'Temperature- Battery':4,'SOC':20})
        g.tick(self.now,sample)
        g.client.set_battery_parameter.assert_called_once_with('TEST','MAX_CHARGE_CURRENT',0)

    def test_away_is_explicit_and_reserves_60_even_when_warm(self):
        c=cfg('winter-away');p=build_plan(c,{},[(self.now,20,100)],self.now)
        apply_plan(c,p)
        self.assertEqual(p['reserve_soc_pct'],60)
        self.assertEqual(c['battery']['soc_floor_pct'],60)
        self.assertEqual(c['grid']['day_export_w'],0)
        self.assertEqual(c['house']['mode'],'winter-away')

    def test_missing_or_stale_forecast_never_releases_a_reserve(self):
        for weather in [None, {**self.weather(), 'fetched_at':(self.now-dt.timedelta(hours=4)).isoformat()},
                        {**self.weather(), 'points':self.weather()['points'][:6]}]:
            p=build_plan(cfg(),{'reserve_soc_pct':60},[(self.now,20,100)],self.now,weather)
            self.assertEqual(p['reserve_soc_pct'],60)

    def test_warming_must_last_24_hours_before_releasing_reserve(self):
        p=build_plan(cfg(),{'reserve_soc_pct':60},[(self.now,20,100)],self.now,self.weather())
        self.assertEqual(p['reserve_soc_pct'],60)
        self.assertIn('warming_since',p)
        p['warming_since']=(self.now-dt.timedelta(hours=25)).isoformat()
        p=build_plan(cfg(),p,[(self.now,20,100)],self.now,self.weather())
        self.assertEqual(p['reserve_soc_pct'],20)

    def test_grid_current_hysteresis_and_cold_alert(self):
        for temp,old,current in [(6,10,5),(8,5,5),(9,5,10)]:
            p=build_plan(cfg('winter-away'),{'grid_charge_a':old},[(self.now,temp,100)],self.now)
            self.assertEqual(p['grid_charge_a'],current)
        p=build_plan(cfg('winter-away'),{},[(self.now,4,100)],self.now)
        self.assertIn('charging_blocked_cold',p['alerts'])

    def test_custom_maintenance_has_a_distinct_alert_and_retains_sixty_reserve(self):
        c=cfg();c['charge_temperature'].update(cold_maintenance_enabled=True,cold_maintenance_min_c=2)
        p=build_plan(c,{},[(self.now,4,100)],self.now)
        self.assertEqual(p['reserve_soc_pct'],60)
        self.assertEqual(p['grid_charge_a'],5)
        self.assertIn('cold_maintenance_only',p['alerts'])
        p=build_plan(c,{},[(self.now,1,100)],self.now)
        self.assertIn('charging_blocked_cold',p['alerts'])

    def test_away_sets_storage_reserve_and_preserves_other_native_fields(self):
        current={'touAction':'on','timeUseSettingItems':[{'time':t,'soc':15,'power':12000,'voltage':49,
                 'enableGridCharge':True,'enableGeneration':False,'enableSell':False}
                 for t in ['01:00','07:00','09:00','13:00','17:00','21:00']]}
        c=cfg('winter-away');apply_plan(c,{})
        request=reserve_request(c,current)
        self.assertTrue(all(x['power']==12000 and x['soc']==60 for x in request['timeUseSettingItems']))
        c=cfg()
        request=reserve_request(c,{**current,'timeUseSettingItems':request['timeUseSettingItems']})
        self.assertTrue(all(x['power']==12000 for x in request['timeUseSettingItems']))

    def test_controller_never_schedules_exports_in_away_mode(self):
        ctl=Controller.__new__(Controller);ctl.cfg=Config(cfg('winter-away'))
        self.assertEqual(ctl.classify_and_decide(self.now,80),('STORAGE',0,None,'winter_away_storage'))

    def guard(self,mode='winter-away',current=90,hold=False):
        from test_charge_temperature import MemoryDB
        c=cfg(mode);db=MemoryDB({'confirmed_a':current,'storage_charge_hold':hold})
        client=Mock();client.set_battery_parameter.return_value={'orderId':123}
        return ChargeTemperatureGuard(c,db,client,Mock())

    def test_away_stops_solar_charging_at_storage_target(self):
        from types import SimpleNamespace
        g=self.guard();s=SimpleNamespace(collection_at=self.now,metrics={'Temperature- Battery':14,'SOC':60})
        g.tick(self.now,s)
        self.assertTrue(g.state['storage_charge_hold'])
        g.client.set_battery_parameter.assert_called_once_with('TEST','MAX_CHARGE_CURRENT',0)

    def test_away_hold_survives_restart_and_resumes_only_below_55(self):
        from types import SimpleNamespace
        g=self.guard(current=0,hold=True)
        for soc,hold in [(58,True),(55,False)]:
            s=SimpleNamespace(collection_at=self.now,metrics={'Temperature- Battery':14,'SOC':soc})
            g.tick(self.now,s);self.assertEqual(g.state['storage_charge_hold'],hold)

    def test_low_soc_does_not_override_cold_cutoff(self):
        from types import SimpleNamespace
        g=self.guard();s=SimpleNamespace(collection_at=self.now,metrics={'Temperature- Battery':4,'SOC':10})
        g.tick(self.now,s)
        g.client.set_battery_parameter.assert_called_once_with('TEST','MAX_CHARGE_CURRENT',0)

    def test_occupied_clears_storage_hold(self):
        from types import SimpleNamespace
        g=self.guard(mode='occupied',hold=True)
        g.tick(self.now,SimpleNamespace(collection_at=self.now,metrics={'Temperature- Battery':14,'SOC':60}))
        self.assertNotIn('storage_charge_hold',g.state)

    def test_config_accepts_60_percent_storage_and_retains_manual_mode(self):
        with tempfile.TemporaryDirectory() as td,patch.dict(os.environ,{},clear=True):
            p=Path(td)/'deye.env'
            p.write_text('\n'.join(['DEYE_APP_ID=id','DEYE_APP_SECRET=s','DEYE_LOGIN=l','DEYE_PASSWORD=p',
              'DEYE_INVERTER_SN=TEST','SEASON_MODE=winter','BATTERY_TEMP_CHARGE_ENABLED=true',
              'HOUSE_RESERVE_ENABLED=true','HOUSE_MODE=winter-away',
              'LOGGING_STATE_DB='+str(Path(td)/'state.db'),
              'PV_ARRAYS_JSON=[{"kwp":1,"tilt_deg":0,"azimuth_deg":0}]']))
            c=load_config(str(p));self.assertEqual(c['battery']['soc_floor_pct'],60)
            before=p.read_text();backup=update_mode(p,'occupied')
            self.assertEqual(backup.read_text(),before)
            c=load_config(str(p));self.assertEqual(c['house']['mode'],'occupied')
            self.assertEqual(c['battery']['soc_floor_pct'],15)

    def test_unconfirmed_native_plan_is_not_published_to_controller(self):
        import season_manager
        from state_db import StateDB
        with tempfile.TemporaryDirectory() as td:
            c=cfg();c['season'].update(active='winter',mode='auto')
            c['logging']={'state_db':str(Path(td)/'state.db')};c['site']={'timezone':'UTC'}
            db=StateDB(c['logging']['state_db']);db.close()
            plan={'mode':'occupied','reserve_soc_pct':50,'grid_charge_a':10}
            with patch('season_manager.load_config',return_value=c), \
                 patch('house_policy.fetch_weather',return_value=self.weather()), \
                 patch('house_policy.battery_history',return_value=[]), \
                 patch('house_policy.build_plan',return_value=plan), \
                 patch('season_manager.service_active',return_value=True), \
                 patch('season_manager.service') as service, \
                 patch('season_manager.check_control_idle'), \
                 patch('season_manager.make_deye_client'), \
                 patch('season_manager.apply_native',side_effect=RuntimeError('unconfirmed')), \
                 patch('sys.argv',['season','--apply']):
                with self.assertRaisesRegex(RuntimeError,'unconfirmed'):season_manager.main()
                self.assertFalse((Path(td)/'house-state.json').exists())
                self.assertEqual([call.args[0] for call in service.call_args_list],['stop','start'])

    def test_observed_weather_refresh_does_not_reference_another_methods_forecast(self):
        ctl=Controller.__new__(Controller)
        ctl.cfg=Config({'site':{'latitude':0,'longitude':0,'timezone':'UTC'},
                        'pv':{'performance_ratio':0.82}})
        ctl.db=Mock();ctl.db.get.return_value=None;ctl.log=Mock()
        yesterday=self.now.date()-dt.timedelta(days=1)
        with patch('controller.fetch_observed_irradiance',return_value={yesterday:10}):
            ctl.refresh_observed_irradiance(self.now,[])
        ctl.db.set_observed_irradiance.assert_called_once_with(yesterday,10,self.now)
