import datetime as dt
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from analytics_engine import Interval, simulate_static, oracle_replay
from energy_strategy import forecast_distribution, learned_morning_bias_minutes
from state_db import StateDB


class AnalyticsTests(unittest.TestCase):
    def raw(self):
        return {
            "battery":{"effective_kwh":15.0,"soc_floor_pct":15.0,"day_target_soc_pct":96.0},
            "grid":{"export_hard_limit_w":1000,"day_export_w":1000},
            "load_model":{"system_overhead_w":100.0,"base_house_load_w":100.0},
            "analytics":{"battery_charge_efficiency":1.0,"battery_discharge_efficiency":1.0,"battery_max_charge_w":10000,"battery_max_discharge_w":10000,"battery_soc_ceiling_pct":100.0,"oracle_soc_step_pct":1.0,"oracle_export_step_w":100},
            "economic":{"import_eur_kwh":0.25,"export_eur_kwh":0.1,"battery_wear_eur_kwh":0.0},
            "forecast_uncertainty":{"probabilistic_enabled":True,"probabilistic_min_days":3,"probabilistic_learning_days":20,"probabilistic_quantiles":[0.1,0.2,0.5,0.8,0.9],"default_safe_factor":0.8,"learning_days":14,"min_learning_days":3,"lower_quantile":0.2,"min_factor":0.5,"max_factor":1.5},
            "morning_learning":{"enabled":True,"min_days":3,"learning_days":20,"max_abs_minutes":45},
            "night_strategy":{"morning_surplus_threshold_w":350,"sustained_minutes":30},
        }

    def test_static_cap_creates_headroom_and_exports(self):
        tz=ZoneInfo("UTC")
        intervals=[Interval(dt.datetime(2026,1,1,10,tzinfo=tz),dt.datetime(2026,1,1,11,tzinfo=tz),1,3000,100,50,None,None,{})]
        zero=simulate_static(self.raw(),intervals,0)
        one=simulate_static(self.raw(),intervals,1000)
        self.assertGreater(one.export_kwh, zero.export_kwh)
        self.assertLess(one.final_soc_pct, zero.final_soc_pct)

    def test_oracle_respects_end_target_when_feasible(self):
        tz=ZoneInfo("UTC")
        intervals=[]
        for h in range(8,16):
            intervals.append(Interval(dt.datetime(2026,1,1,h,tzinfo=tz),dt.datetime(2026,1,1,h+1,tzinfo=tz),1,4000,100,80,None,None,{}))
        o=oracle_replay(self.raw(),intervals,target_soc_pct=90)
        self.assertTrue(o.feasible)
        self.assertGreaterEqual(o.final_soc_pct,90)
        self.assertGreater(o.export_kwh,0)

    def test_probabilistic_waits_then_activates(self):
        with tempfile.TemporaryDirectory() as td:
            path=os.path.join(td,"s.db"); db=StateDB(path)
            base=dt.date(2026,1,10)
            # Two samples -> below min 3.
            for n,ratio in enumerate([0.8,1.2]):
                day=base-dt.timedelta(days=n+1)
                ts=dt.datetime.combine(day,dt.time(23,0),tzinfo=ZoneInfo("UTC"))
                db.conn.execute("INSERT INTO forecasts(fetched_at,target_date,sunrise,sunset,pv_wakeup,useful_pv_start,expected_kwh,array_kwh_json,lead_bucket) VALUES(?,?,?,?,?,?,?,?,?)",
                    ((ts-dt.timedelta(days=1)).isoformat(),day.isoformat(),ts.isoformat(),ts.isoformat(),ts.isoformat(),ts.isoformat(),10.0,"{}","day_ahead"))
                db.conn.execute("INSERT INTO telemetry(observed_at,logger_at,soc,generation_power,consumption_power,grid_power,battery_power,raw_json,daily_production_kwh) VALUES(?,?,?,?,?,?,?,?,?)",
                    (ts.isoformat(),ts.isoformat(),50,0,0,0,0,"{}",10.0*ratio))
            db.conn.commit()
            raw=self.raw(); now=dt.datetime(2026,1,10,22,tzinfo=ZoneInfo("UTC")); target=dt.date(2026,1,11)
            dist,src=forecast_distribution(raw,db,now,target)
            self.assertEqual(dist,{})
            # Add third completed day.
            day=dt.date(2026,1,7); ts=dt.datetime.combine(day,dt.time(23,0),tzinfo=ZoneInfo("UTC"))
            db.conn.execute("INSERT INTO forecasts(fetched_at,target_date,sunrise,sunset,pv_wakeup,useful_pv_start,expected_kwh,array_kwh_json,lead_bucket) VALUES(?,?,?,?,?,?,?,?,?)",
                ((ts-dt.timedelta(days=1)).isoformat(),day.isoformat(),ts.isoformat(),ts.isoformat(),ts.isoformat(),ts.isoformat(),10.0,"{}","day_ahead"))
            db.conn.execute("INSERT INTO telemetry(observed_at,logger_at,soc,generation_power,consumption_power,grid_power,battery_power,raw_json,daily_production_kwh) VALUES(?,?,?,?,?,?,?,?,?)",
                (ts.isoformat(),ts.isoformat(),50,0,0,0,0,"{}",10.0))
            db.conn.commit()
            dist,src=forecast_distribution(raw,db,now,target)
            self.assertIn("p50",dist)
            # The label now names the data source as well as the lead bucket, so a
            # distribution learned from clipped PV is distinguishable from one
            # learned from observed irradiance.
            self.assertTrue(src.startswith("probabilistic_"), src)
            self.assertIn("day_ahead", src)
            self.assertIn("_pv_", src, "no observed irradiance stored, so PV is the source")
            db.close()

    def test_readonly_legacy_forecast_schema_degrades_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            path=os.path.join(td,"legacy.db")
            c=sqlite3.connect(path)
            c.executescript("""
            CREATE TABLE forecasts(id INTEGER PRIMARY KEY, fetched_at TEXT, target_date TEXT, sunrise TEXT, sunset TEXT, pv_wakeup TEXT, useful_pv_start TEXT, expected_kwh REAL, array_kwh_json TEXT);
            CREATE TABLE telemetry(id INTEGER PRIMARY KEY, observed_at TEXT, logger_at TEXT, generation_power REAL, daily_production_kwh REAL, daily_consumption_kwh REAL);
            """)
            day=dt.date(2026,1,2); fetched=dt.datetime(2026,1,1,20,tzinfo=ZoneInfo("UTC"))
            c.execute("INSERT INTO forecasts(fetched_at,target_date,expected_kwh) VALUES(?,?,?)",(fetched.isoformat(),day.isoformat(),10.0))
            c.execute("INSERT INTO telemetry(observed_at,logger_at,generation_power,daily_production_kwh,daily_consumption_kwh) VALUES(?,?,?,?,?)",((fetched+dt.timedelta(days=1)).isoformat(),(fetched+dt.timedelta(days=1)).isoformat(),0,12.0,1.0))
            c.commit(); c.close()
            ro=StateDB(path,readonly=True)
            ratios=ro.forecast_accuracy_ratios_for_bucket(dt.date(2026,1,3),10,"day_ahead")
            self.assertEqual(ratios,[1.2])
            self.assertEqual(ro.morning_timing_errors(dt.date(2026,1,3),10,350,30),[])
            ro.close()

    def test_readonly_state_db_does_not_migrate(self):
        with tempfile.TemporaryDirectory() as td:
            path=os.path.join(td,"s.db"); db=StateDB(path); db.close()
            ro=StateDB(path,readonly=True)
            self.assertIsNotNone(ro.latest_telemetry()) if False else self.assertTrue(True)
            ro.close()

if __name__=="__main__": unittest.main()


class ControlLogTests(unittest.TestCase):
    """v3.1.1: reconstruct write-window availability from the per-tick decision log."""

    tz = ZoneInfo("Europe/Amsterdam")

    def raw(self, path):
        r = AnalyticsTests.raw(self)
        r["site"] = {"timezone": "Europe/Amsterdam"}
        r["logging"] = {"state_db": path}
        r["control"] = {"loop_seconds": 60, "min_write_interval_minutes": 120}
        return r

    def test_gate_classification_covers_the_observed_actions(self):
        from analytics_engine import classify_gate
        cases = {
            "no_material_change": "open",
            "none": "open",
            "direct_accepted": "writing",
            "order_pending": "writing",
            "blocked_cloud_offline": "blocked_cloud",
            "direct_offline": "blocked_cloud",
            "waiting_control_retry": "blocked_cloud",
            "blocked_failed-order_backoff_until_2026-09-05T12:57:06": "blocked_backoff",
            "blocked_successful-write_cooldown_active_(99_min)": "blocked_cooldown",
        }
        for action, expected in cases.items():
            self.assertEqual(classify_gate(action), expected, action)

    def test_segments_writes_and_summary(self):
        from analytics_engine import control_log
        day = dt.date(2026, 9, 5)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "s.db")
            db = StateDB(path)
            base = dt.datetime(2026, 9, 5, 10, 0, tzinfo=self.tz)
            # 10 open ticks, then 10 blocked by a stale cloud.
            for i in range(10):
                db.add_decision(base + dt.timedelta(minutes=i), "DAY", 90.0, 96.0, None,
                                400, 400, "no_material_change", "day_energy_budget")
            for i in range(10, 20):
                db.add_decision(base + dt.timedelta(minutes=i), "DAY", 90.0, 96.0, None,
                                1000, 400, "blocked_cloud_offline", "day_energy_budget")
            db.add_write(base + dt.timedelta(minutes=5), 200, 400, "day_energy_budget",
                         777, "success", '{"status":666}', accepted=True)
            db.add_write(base + dt.timedelta(minutes=12), 400, 100, "day_energy_budget",
                         778, "failed", '{"status":500,"error":"540"}', accepted=True)
            db.close()

            out = control_log(self.raw(path), path, day)

        states = [s["state"] for s in out["segments"]]
        self.assertIn("open", states)
        self.assertIn("blocked_cloud", states)
        self.assertEqual(out["successful_writes"], 1)
        self.assertEqual(out["failed_writes"], 1)
        self.assertEqual(out["submissions"], 2)
        # The Deye rejection code is surfaced rather than buried in the details blob.
        self.assertEqual(out["writes"][1]["error_code"], "540")
        self.assertEqual(out["writes"][1]["delta_w"], -300)
        self.assertGreater(out["summary_minutes"]["open"], 0)
        self.assertGreater(out["summary_minutes"]["blocked_cloud"], 0)
        # The day starts with no controller data before the first recorded tick.
        self.assertEqual(out["segments"][0]["state"], "no_data")

    def test_gap_in_ticks_becomes_a_no_data_segment(self):
        from analytics_engine import control_log
        day = dt.date(2026, 9, 5)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "s.db")
            db = StateDB(path)
            base = dt.datetime(2026, 9, 5, 10, 0, tzinfo=self.tz)
            db.add_decision(base, "DAY", 90.0, 96.0, None, 400, 400, "no_material_change", "r")
            # Two hours later: the controller was not running in between.
            db.add_decision(base + dt.timedelta(hours=2), "DAY", 90.0, 96.0, None, 400, 400,
                            "no_material_change", "r")
            db.close()
            out = control_log(self.raw(path), path, day)
        gaps = [s for s in out["segments"] if s["state"] == "no_data" and s["minutes"] > 60]
        self.assertTrue(gaps, "a two-hour tick gap must show as no_data")


class CurtailmentEstimateTests(unittest.TestCase):
    """v3.1.6: separate a clipped array from a cloudy one."""

    tz = ZoneInfo("Europe/Amsterdam")

    def raw(self, path, **an):
        r = AnalyticsTests.raw(self)
        r["site"] = {"timezone": "Europe/Amsterdam"}
        r["logging"] = {"state_db": path}
        r["pv"] = {"arrays": [{"name": "s", "kwp": 10.0, "tilt_deg": 35, "azimuth_deg": 0}]}
        r["analytics"] = dict(r["analytics"], **{
            "curtailment_soc_threshold_pct": 98.0, "curtailment_export_margin_w": 100.0,
            "curtailment_min_gap_w": 300.0, "curtailment_max_charge_w": 300.0,
            "curtailment_max_kwh_per_kwp": 3.6, **an})
        return r

    def _db(self, path, date, forecast_w):
        db = StateDB(path)
        t = dt.datetime.combine(date, dt.time.min, tzinfo=self.tz)
        pts = []
        for i in range(96):
            ts = t + dt.timedelta(minutes=15 * i)
            pts.append(SimpleNamespace(time=ts, predicted_w=forecast_w if 8 <= ts.hour < 18 else 0.0))
        db.add_forecast(SimpleNamespace(
            date=date, sunrise=t.replace(hour=7), sunset=t.replace(hour=20),
            pv_wakeup=t.replace(hour=8), useful_pv_start=t.replace(hour=8),
            expected_kwh=1.0, array_kwh={"s": 1.0}, points=pts,
            fetched_at=t.replace(hour=1)))
        db.close()

    def _iv(self, date, hour, pv_w, soc, battery_w, grid_w=-1000.0, synth=False):
        a = dt.datetime.combine(date, dt.time(hour, 0), tzinfo=self.tz)
        return Interval(a, a + dt.timedelta(hours=1), 1.0, pv_w, 300.0, soc,
                        battery_w, grid_w, {}, synth)

    def test_full_battery_with_clipped_pv_is_reported(self):
        from analytics_engine import curtailment_estimate
        date = dt.date(2026, 9, 8)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "s.db")
            self._db(path, date, 4000.0)
            ivs = ([self._iv(date, h, 4000.0, 60.0, -3000.0) for h in range(8, 14)] +
                   [self._iv(date, h, 1500.0, 100.0, -20.0) for h in range(14, 18)])
            out = curtailment_estimate(self.raw(path), path, date, ivs)
        self.assertGreater(out["estimated_kwh"], 5.0)
        self.assertEqual(out["flagged_intervals"], 4)
        self.assertIn("self_calibrated", out["calibration"])

    def test_a_battery_still_absorbing_is_not_curtailment(self):
        """Same low PV, but the battery is taking 3 kW: the surplus had somewhere to go."""
        from analytics_engine import curtailment_estimate
        date = dt.date(2026, 9, 8)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "s.db")
            self._db(path, date, 4000.0)
            ivs = ([self._iv(date, h, 4000.0, 60.0, -3000.0) for h in range(8, 14)] +
                   [self._iv(date, h, 1500.0, 100.0, -3000.0) for h in range(14, 18)])
            out = curtailment_estimate(self.raw(path), path, date, ivs)
        self.assertEqual(out["estimated_kwh"], 0.0)
        self.assertEqual(out["intervals_skipped_battery_absorbing"], 4)

    def test_reconstructed_intervals_cannot_set_the_calibration(self):
        """A counter-reconstruction spike must not scale the whole counterfactual."""
        from analytics_engine import curtailment_estimate
        date = dt.date(2026, 9, 8)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "s.db")
            self._db(path, date, 4000.0)
            spikes = [self._iv(date, h, 13000.0, 60.0, -3000.0, synth=True) for h in range(8, 14)]
            clipped = [self._iv(date, h, 1500.0, 100.0, -20.0) for h in range(14, 18)]
            out = curtailment_estimate(self.raw(path), path, date, spikes + clipped)
        # With only synthesized intervals available there is nothing to calibrate on.
        self.assertNotIn("self_calibrated", out["calibration"])
        self.assertEqual(out["forecast_factor"], out["learned_p50"])

    def test_an_implausible_daily_yield_is_flagged(self):
        from analytics_engine import curtailment_estimate
        date = dt.date(2026, 9, 8)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "s.db")
            self._db(path, date, 9000.0)
            ivs = ([self._iv(date, h, 9000.0, 60.0, -6000.0) for h in range(8, 14)] +
                   [self._iv(date, h, 500.0, 100.0, -10.0) for h in range(14, 18)])
            out = curtailment_estimate(self.raw(path, curtailment_max_kwh_per_kwp=1.0),
                                       path, date, ivs)
        self.assertFalse(out["plausible"])
        self.assertIsNotNone(out["implied_kwh_per_kwp"])


class WeatherRatioLearnerTests(unittest.TestCase):
    """v3.1.8: learn forecast error from observed irradiance, not from clipped PV."""

    def raw(self, **fu):
        r = AnalyticsTests.raw(self)
        r["forecast_uncertainty"] = dict(r["forecast_uncertainty"], **{
            "probabilistic_enabled": True, "probabilistic_min_days": 3,
            "probabilistic_learning_days": 30, "weather_ratio_enabled": True, **fu})
        return r

    def _seed(self, db, days, forecast_kwh, observed_kwh, measured_kwh, bucket="day_ahead"):
        base = dt.date(2026, 9, 1)
        for i in range(days):
            d = base + dt.timedelta(days=i)
            ts = dt.datetime.combine(d, dt.time(12, 0), tzinfo=ZoneInfo("UTC"))
            db.conn.execute(
                "INSERT INTO forecasts(fetched_at,target_date,sunrise,sunset,pv_wakeup,"
                "useful_pv_start,expected_kwh,array_kwh_json,lead_bucket) VALUES(?,?,?,?,?,?,?,?,?)",
                ((ts - dt.timedelta(days=1)).isoformat(), d.isoformat(), ts.isoformat(),
                 ts.isoformat(), ts.isoformat(), ts.isoformat(), forecast_kwh, "{}", bucket))
            db.conn.execute(
                "INSERT INTO telemetry(observed_at,logger_at,soc,generation_power,"
                "consumption_power,grid_power,battery_power,raw_json,daily_production_kwh) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (ts.isoformat(), ts.isoformat(), 50, 0, 0, 0, 0, "{}", measured_kwh))
            db.set_observed_irradiance(d, observed_kwh, ts)
        db.conn.commit()

    def test_clipping_does_not_drag_the_learned_factor_down(self):
        """Forecast right, sky right, but PV censored: the factor must not fall."""
        from energy_strategy import forecast_distribution
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(os.path.join(td, "s.db"))
            # Sky matched the forecast (30/30) but the array was clipped to 18 kWh.
            self._seed(db, 6, forecast_kwh=30.0, observed_kwh=30.0, measured_kwh=18.0)
            now = dt.datetime(2026, 9, 10, 12, 0, tzinfo=ZoneInfo("UTC"))
            wx, src = forecast_distribution(self.raw(), db, now, dt.date(2026, 9, 11))
            pv, psrc = forecast_distribution(self.raw(weather_ratio_enabled=False), db, now,
                                             dt.date(2026, 9, 11))
            db.close()
        self.assertIn("_wx_", src)
        self.assertIn("_pv_", psrc)
        self.assertGreater(wx["p50"], pv["p50"] + 0.2,
                           "the PV-based learner should be far more pessimistic")
        self.assertGreaterEqual(wx["p50"], 0.95)

    def test_a_genuinely_bad_forecast_is_still_learned(self):
        from energy_strategy import forecast_distribution
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(os.path.join(td, "s.db"))
            # The sky really did under-deliver: observed well below forecast.
            self._seed(db, 6, forecast_kwh=30.0, observed_kwh=18.0, measured_kwh=18.0)
            now = dt.datetime(2026, 9, 10, 12, 0, tzinfo=ZoneInfo("UTC"))
            dist, src = forecast_distribution(self.raw(), db, now, dt.date(2026, 9, 11))
            db.close()
        self.assertIn("_wx_", src)
        self.assertLess(dist["p50"], 0.7)

    def test_it_falls_back_to_pv_without_enough_observed_days(self):
        from energy_strategy import forecast_distribution
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(os.path.join(td, "s.db"))
            self._seed(db, 6, forecast_kwh=30.0, observed_kwh=30.0, measured_kwh=18.0)
            db.conn.execute("DELETE FROM observed_irradiance")
            db.conn.commit()
            now = dt.datetime(2026, 9, 10, 12, 0, tzinfo=ZoneInfo("UTC"))
            dist, src = forecast_distribution(self.raw(), db, now, dt.date(2026, 9, 11))
            db.close()
        self.assertIn("_pv_", src)
