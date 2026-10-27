import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from house_policy import build_plan, solar_opportunity, apply_plan
from seasonal_policy import reserve_request
from state_db import StateDB
from test_house_policy import cfg


class HouseSolarTests(unittest.TestCase):
    now=dt.datetime(2026,1,10,0,tzinfo=dt.timezone.utc)

    def weather(self, current=10, minimum=5):
        return {'fetched_at':self.now.isoformat(),'source':'synthetic', 'current_air_c':current,
                'points':[((self.now+dt.timedelta(hours=h)).isoformat(),minimum) for h in range(49)]}

    def solar(self, energy=5, **overrides):
        return {'valid':True,'fetched_at':self.now.isoformat(),
                'refill_at':(self.now+dt.timedelta(hours=17)).isoformat(),
                'stored_surplus_kwh':energy,**overrides}

    def plan(self, temperature=14, previous=None, weather=None, solar=None, mode='occupied'):
        return build_plan(cfg(mode),previous or {},[(self.now,temperature,100)],self.now,
                          weather if weather is not None else self.weather(),
                          solar if solar is not None else self.solar())

    def test_sunny_morning_defers_forecast_extra_but_retains_measured_thirty_percent(self):
        p=self.plan()
        self.assertEqual(p['cold_target_soc_pct'],50)
        self.assertEqual(p['measured_min_soc_pct'],30)
        self.assertEqual(p['reserve_soc_pct'],30)
        self.assertEqual(p['solar_deferral_kwh'],3)

    def test_partial_solar_surplus_defers_only_the_energy_it_can_cover(self):
        p=self.plan(solar=self.solar(1.5))
        self.assertEqual(p['reserve_soc_pct'],40)
        self.assertEqual(p['solar_deferral_kwh'],1.5)

    def test_cloudy_day_keeps_full_forecast_reserve(self):
        self.assertEqual(self.plan(solar=self.solar(0))['reserve_soc_pct'],50)

    def test_missing_stale_future_or_bad_solar_does_not_release_reserve(self):
        for solar in [{'valid':False},self.solar(fetched_at=(self.now-dt.timedelta(hours=4)).isoformat()),
                      self.solar(fetched_at=(self.now+dt.timedelta(minutes=1)).isoformat()),
                      self.solar(float('nan')),self.solar(refill_at=self.now.isoformat())]:
            self.assertEqual(self.plan(solar=solar)['reserve_soc_pct'],50)

    def test_measured_cold_and_imminent_cutoff_cannot_be_deferred(self):
        self.assertEqual(self.plan(temperature=9)['reserve_soc_pct'],60)
        p=self.plan(weather=self.weather(minimum=0))
        self.assertEqual(p['reserve_soc_pct'],60)
        self.assertEqual(p['solar_deferral_kwh'],0)

    def test_winter_away_always_keeps_storage_reserve(self):
        self.assertEqual(self.plan(mode='winter-away')['reserve_soc_pct'],60)

    def test_warming_hold_is_retained_as_a_goal_without_forcing_grid_topup(self):
        p=self.plan(previous={'reserve_soc_pct':50},weather=self.weather(minimum=10))
        self.assertEqual(p['cold_target_soc_pct'],50)
        self.assertEqual(p['reserve_soc_pct'],30)
        self.assertIn('warming_since',p)
        q=build_plan(cfg(),p,[(self.now,14,100)],self.now,None,self.solar())
        self.assertEqual(q['reserve_soc_pct'],50)

    def test_solar_adjustment_reaches_all_native_reserve_slots(self):
        c=cfg();p=self.plan();apply_plan(c,p)
        native={'touAction':'on','timeUseSettingItems':[dict(time=t,soc=50,power=12000,voltage=49,
                  enableGridCharge=True,enableGeneration=False,enableSell=False)
                  for t in ['01:00','07:00','09:00','13:00','17:00','21:00']]}
        request=reserve_request(c,native)
        self.assertTrue(all(s['soc']==30 for s in request['timeUseSettingItems']))

    def forecast(self, db, power=2000, fetched=None, gap=False):
        points=[SimpleNamespace(time=self.now+dt.timedelta(minutes=15*i),predicted_w=power) for i in range(97)]
        if gap:points=[p for p in points if not 10 <= p.time.hour <= 11]
        db.add_forecast(SimpleNamespace(fetched_at=fetched or self.now,date=self.now.date(),
             sunrise=self.now+dt.timedelta(hours=7),sunset=self.now+dt.timedelta(hours=18),
             pv_wakeup=self.now+dt.timedelta(hours=8),useful_pv_start=self.now+dt.timedelta(hours=8),
             expected_kwh=20,array_kwh={'synthetic':20},points=points))

    def opportunity(self,power=2000,**options):
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(str(Path(td)/'state.db'));self.forecast(db,power,options.pop('fetched',None),options.pop('gap',False))
            c=cfg();c['site']={'timezone':'UTC'}
            c['water_heater']={'enabled':True,'scheduled_energy_kwh':options.pop('appliance',0)}
            p=solar_opportunity(c,db,self.now,options.pop('temperature',14));db.close();return p

    def test_solar_budget_discounts_forecast_and_subtracts_house_and_appliances(self):
        p=self.opportunity(appliance=2)
        self.assertTrue(p['valid'])
        self.assertEqual(p['factor'],0.7)
        self.assertAlmostEqual(p['safe_pv_kwh'],12.6)
        self.assertAlmostEqual(p['load_kwh'],5.24)
        self.assertAlmostEqual(p['stored_surplus_kwh'],6.698,places=3)

    def test_no_surplus_from_clouds_or_large_scheduled_loads(self):
        self.assertEqual(self.opportunity(power=400)['stored_surplus_kwh'],0)
        self.assertEqual(self.opportunity(appliance=30)['stored_surplus_kwh'],0)

    def test_stale_future_and_incomplete_forecasts_are_rejected(self):
        self.assertFalse(self.opportunity(fetched=self.now-dt.timedelta(hours=4))['valid'])
        self.assertFalse(self.opportunity(fetched=self.now+dt.timedelta(minutes=1))['valid'])
        self.assertFalse(self.opportunity(gap=True)['valid'])

    def test_temperature_charging_ceiling_limits_solar_credit(self):
        p=self.opportunity(power=10000)
        self.assertLessEqual(p['stored_surplus_kwh'],90*51.2/1000*9*0.91+0.001)
        self.assertEqual(self.opportunity(temperature=4)['stored_surplus_kwh'],0)

    def test_completed_appliance_is_not_counted_again_in_remaining_solar_budget(self):
        with tempfile.TemporaryDirectory() as td:
            db=StateDB(str(Path(td)/'state.db'));self.forecast(db)
            c=cfg();c['site']={'timezone':'UTC'}
            c['water_heater']={'enabled':True,'scheduled_time':'09:00','duration_minutes':60,'scheduled_energy_kwh':3}
            now=self.now+dt.timedelta(hours=12)
            # Keep the forecast fresh for this daytime calculation.
            self.forecast(db,fetched=now)
            p=solar_opportunity(c,db,now,14)
            self.assertEqual(p['load_kwh'],1.8)
            self.assertAlmostEqual(p['stored_surplus_kwh'],4.732,places=3)
            db.close()
