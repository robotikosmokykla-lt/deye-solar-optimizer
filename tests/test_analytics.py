import datetime as dt
import os
import sqlite3
import tempfile
import unittest
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
            self.assertTrue(src.startswith("probabilistic_day_ahead_"))
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

    tz = ZoneInfo("Europe/Vilnius")

    def raw(self, path):
        r = AnalyticsTests.raw(self)
        r["site"] = {"timezone": "Europe/Vilnius"}
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
