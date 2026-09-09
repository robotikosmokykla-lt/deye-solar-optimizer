#!/usr/bin/env python3
"""Read-only analytics for Deye Solar Optimizer v3.1.0.

This module NEVER calls the Deye control API. It replays local telemetry, compares
counterfactual export caps, builds a perfect-foresight benchmark, estimates forecast
quality/curtailment, tracks battery stress and exposes MPPT/economic summaries.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from energy_strategy import _quantile, forecast_distribution
from state_db import StateDB, forecast_lead_bucket


@dataclass
class Interval:
    start: dt.datetime
    end: dt.datetime
    hours: float
    pv_w: float
    load_w: float
    soc_pct: Optional[float]
    battery_w: Optional[float]
    grid_w: Optional[float]
    mppt_w: Dict[str, float]
    synthesized_from_counters: bool = False


@dataclass
class SimResult:
    label: str
    cap_w: int
    export_kwh: float = 0.0
    import_kwh: float = 0.0
    charge_input_kwh: float = 0.0
    discharge_output_kwh: float = 0.0
    curtailed_kwh: float = 0.0
    final_soc_pct: float = 0.0
    min_soc_pct: float = 100.0
    max_soc_pct: float = 0.0
    battery_throughput_kwh: float = 0.0
    equivalent_full_cycles: float = 0.0
    economic_cost_eur: float = 0.0
    feasible: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _raw_metrics(raw_json: str | None) -> Dict[str, Any]:
    if not raw_json:
        return {}
    try:
        data = json.loads(raw_json)
    except Exception:
        return {}
    if isinstance(data, dict) and data.get("deviceDataList"):
        dev = data["deviceDataList"][0]
        return {str(x.get("key")): x.get("value") for x in dev.get("dataList", []) if x.get("key")}
    return data if isinstance(data, dict) else {}


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _delta(a: Any, b: Any, *, max_delta: float = 1000.0) -> Optional[float]:
    aa, bb = _f(a), _f(b)
    if aa is None or bb is None:
        return None
    d = bb - aa
    if -1e-6 <= d <= max_delta:
        return max(0.0, d)
    return None


def _dedup_day_rows(c: sqlite3.Connection, date: dt.date) -> list[sqlite3.Row]:
    rows = c.execute(
        "SELECT * FROM telemetry WHERE logger_at IS NOT NULL AND substr(logger_at,1,10)=? ORDER BY logger_at, id",
        (date.isoformat(),),
    ).fetchall()
    by_logger: dict[str, sqlite3.Row] = {}
    for r in rows:
        by_logger[str(r["logger_at"])] = r  # latest observed duplicate wins
    return [by_logger[k] for k in sorted(by_logger)]


def day_intervals(db_path: str, date: dt.date) -> tuple[list[Interval], Dict[str, Any]]:
    c = _conn(db_path)
    try:
        rows = _dedup_day_rows(c, date)
    finally:
        c.close()
    if len(rows) < 2:
        return [], {"samples": len(rows), "coverage_hours": 0.0, "day_hours": 24.0, "coverage_pct": 0.0}

    intervals: list[Interval] = []
    for a, b in zip(rows, rows[1:]):
        try:
            ta = dt.datetime.fromisoformat(a["logger_at"])
            tb = dt.datetime.fromisoformat(b["logger_at"])
        except Exception:
            continue
        hours = (tb - ta).total_seconds() / 3600.0
        if hours <= 0 or hours > 8.0:
            continue
        pv_delta = _delta(a["daily_production_kwh"], b["daily_production_kwh"], max_delta=100.0)
        load_delta = _delta(a["daily_consumption_kwh"], b["daily_consumption_kwh"], max_delta=100.0)
        synth = False
        if pv_delta is not None:
            pv_w = pv_delta / hours * 1000.0
            synth = hours > 0.30
        else:
            pv_w = max(0.0, statistics.fmean([x for x in (_f(a["generation_power"]), _f(b["generation_power"])) if x is not None]) if any(x is not None for x in (_f(a["generation_power"]), _f(b["generation_power"]))) else 0.0)
        if load_delta is not None:
            load_w = load_delta / hours * 1000.0
            synth = synth or hours > 0.30
        else:
            vals = [x for x in (_f(a["consumption_power"]), _f(b["consumption_power"])) if x is not None]
            load_w = statistics.fmean(vals) if vals else 0.0
        ma, mb = _raw_metrics(a["raw_json"]), _raw_metrics(b["raw_json"])
        mppt: Dict[str, float] = {}
        for i in range(1, 5):
            key = f"DCPowerPV{i}"
            vals = [x for x in (_f(ma.get(key)), _f(mb.get(key))) if x is not None]
            if vals:
                mppt[f"PV{i}"] = max(0.0, statistics.fmean(vals))
        intervals.append(Interval(
            start=ta, end=tb, hours=hours, pv_w=max(0.0, pv_w), load_w=max(0.0, load_w),
            soc_pct=_f(a["soc"]), battery_w=_f(a["battery_power"]), grid_w=_f(a["grid_power"]),
            mppt_w=mppt, synthesized_from_counters=synth,
        ))
    coverage = sum(i.hours for i in intervals)
    return intervals, {
        "samples": len(rows), "intervals": len(intervals), "coverage_hours": round(coverage, 3),
        "day_hours": 24.0, "coverage_pct": round(min(100.0, coverage / 24.0 * 100.0), 1),
        "synthesized_intervals": sum(1 for i in intervals if i.synthesized_from_counters),
    }


def _initial_soc(intervals: list[Interval], fallback: float) -> float:
    for i in intervals:
        if i.soc_pct is not None:
            return float(i.soc_pct)
    return fallback


def _step_policy(
    stored_kwh: float, interval: Interval, export_cap_w: int, raw: Dict[str, Any]
) -> tuple[float, Dict[str, float]]:
    cfg = raw["analytics"]
    batt = raw["battery"]
    capacity = float(batt["effective_kwh"])
    floor_e = capacity * float(batt["soc_floor_pct"]) / 100.0
    ceiling_e = capacity * float(cfg.get("battery_soc_ceiling_pct", 100.0)) / 100.0
    ce = max(0.01, min(1.0, float(cfg.get("battery_charge_efficiency", 0.91))))
    de = max(0.01, min(1.0, float(cfg.get("battery_discharge_efficiency", 0.95))))
    max_ch = max(0.0, float(cfg.get("battery_max_charge_w", 10000.0)))
    max_dis = max(0.0, float(cfg.get("battery_max_discharge_w", 10000.0)))
    h = interval.hours
    overhead = float(raw["load_model"].get("system_overhead_w", 0.0))
    net_w = interval.pv_w - interval.load_w - overhead
    cap = max(0.0, min(float(raw["grid"]["export_hard_limit_w"]), float(export_cap_w)))

    # Battery output needed to cover local deficit and then reach desired export cap.
    need_dis_w = max(0.0, cap - net_w)
    available_dis_w = max(0.0, (stored_kwh - floor_e) * 1000.0 * de / max(h, 1e-9))
    dis_w = min(need_dis_w, max_dis, available_dis_w)
    balance_w = net_w + dis_w
    export_w = max(0.0, min(cap, balance_w))
    import_w = max(0.0, -balance_w)
    stored_kwh -= dis_w / de * h / 1000.0

    surplus_w = max(0.0, net_w - export_w)
    room_input_w = max(0.0, (ceiling_e - stored_kwh) * 1000.0 / ce / max(h, 1e-9))
    charge_w = min(surplus_w, max_ch, room_input_w)
    stored_kwh += charge_w * ce * h / 1000.0
    curtailed_w = max(0.0, surplus_w - charge_w)
    stored_kwh = max(floor_e, min(ceiling_e, stored_kwh))
    return stored_kwh, {
        "export_kwh": export_w * h / 1000.0,
        "import_kwh": import_w * h / 1000.0,
        "charge_input_kwh": charge_w * h / 1000.0,
        "discharge_output_kwh": dis_w * h / 1000.0,
        "curtailed_kwh": curtailed_w * h / 1000.0,
    }


def _economic_cost(raw: Dict[str, Any], export_kwh: float, import_kwh: float, discharge_kwh: float) -> float:
    e = raw.get("economic", {})
    return (
        import_kwh * float(e.get("import_eur_kwh", 0.0))
        - export_kwh * float(e.get("export_eur_kwh", 0.0))
        + discharge_kwh * float(e.get("battery_wear_eur_kwh", 0.0))
    )


def simulate_static(raw: Dict[str, Any], intervals: list[Interval], cap_w: int, *, label: Optional[str] = None) -> SimResult:
    capacity = float(raw["battery"]["effective_kwh"])
    initial_soc = _initial_soc(intervals, float(raw["battery"].get("day_target_soc_pct", 96.0)))
    stored = capacity * initial_soc / 100.0
    result = SimResult(label=label or f"Static {cap_w} W", cap_w=int(cap_w), final_soc_pct=initial_soc, min_soc_pct=initial_soc, max_soc_pct=initial_soc)
    for interval in intervals:
        stored, flows = _step_policy(stored, interval, cap_w, raw)
        for k, v in flows.items():
            setattr(result, k, getattr(result, k) + v)
        soc = stored / capacity * 100.0
        result.min_soc_pct = min(result.min_soc_pct, soc)
        result.max_soc_pct = max(result.max_soc_pct, soc)
    result.final_soc_pct = stored / capacity * 100.0
    result.battery_throughput_kwh = result.charge_input_kwh + result.discharge_output_kwh
    result.equivalent_full_cycles = result.battery_throughput_kwh / max(0.001, 2.0 * capacity)
    result.economic_cost_eur = _economic_cost(raw, result.export_kwh, result.import_kwh, result.discharge_output_kwh)
    return result


def actual_day_metrics(raw: Dict[str, Any], db_path: str, date: dt.date, intervals: list[Interval]) -> Dict[str, Any]:
    c = _conn(db_path)
    try:
        rows = _dedup_day_rows(c, date)
    finally:
        c.close()
    if not rows:
        return {}
    first, last = rows[0], rows[-1]
    export = _delta(first["total_sell_kwh"], last["total_sell_kwh"], max_delta=1000.0)
    imp = _delta(first["total_buy_kwh"], last["total_buy_kwh"], max_delta=1000.0)
    charge = _delta(first["total_charge_kwh"], last["total_charge_kwh"], max_delta=1000.0)
    discharge = _delta(first["total_discharge_kwh"], last["total_discharge_kwh"], max_delta=1000.0)
    production = _delta(first["daily_production_kwh"], last["daily_production_kwh"], max_delta=1000.0)
    consumption = _delta(first["daily_consumption_kwh"], last["daily_consumption_kwh"], max_delta=1000.0)
    # Daily counters often start at zero even when our first logger sample is later; use last value if larger.
    production = max(production or 0.0, _f(last["daily_production_kwh"]) or 0.0)
    consumption = max(consumption or 0.0, _f(last["daily_consumption_kwh"]) or 0.0)
    capacity = float(raw["battery"]["effective_kwh"])
    throughput = (charge or 0.0) + (discharge or 0.0)
    return {
        "label": "Actual optimizer", "export_kwh": export or 0.0, "import_kwh": imp or 0.0,
        "charge_input_kwh": charge or 0.0, "discharge_output_kwh": discharge or 0.0,
        "battery_throughput_kwh": throughput,
        "equivalent_full_cycles": throughput / max(0.001, 2.0 * capacity),
        "pv_kwh": production, "load_kwh": consumption,
        "initial_soc_pct": _f(first["soc"]), "final_soc_pct": _f(last["soc"]),
        "economic_cost_eur": _economic_cost(raw, export or 0.0, imp or 0.0, discharge or 0.0),
        "first_sample": first["logger_at"], "last_sample": last["logger_at"],
    }


def oracle_replay(raw: Dict[str, Any], intervals: list[Interval], target_soc_pct: Optional[float] = None, *, objective: str = "energy") -> SimResult:
    """Perfect-foresight dynamic export-cap benchmark using discretized SOC DP.

    objective="energy" maximizes export-import. objective="economic" maximizes
    export revenue - import cost - configured battery wear cost. Both enforce the
    end-of-day SOC target when feasible and never grid-charge the battery.
    """
    hard = int(raw["grid"]["export_hard_limit_w"])
    step_w = max(50, int(raw["analytics"].get("oracle_export_step_w", 100)))
    soc_step = max(0.25, float(raw["analytics"].get("oracle_soc_step_pct", 1.0)))
    capacity = float(raw["battery"]["effective_kwh"])
    floor = float(raw["battery"]["soc_floor_pct"])
    ceiling = float(raw["analytics"].get("battery_soc_ceiling_pct", 100.0))
    target = float(target_soc_pct if target_soc_pct is not None else raw["battery"].get("day_target_soc_pct", 96.0))
    initial_soc = _initial_soc(intervals, target)
    actions = list(range(0, hard + 1, step_w))
    if actions[-1] != hard:
        actions.append(hard)

    def q_soc(soc: float) -> float:
        return max(floor, min(ceiling, round(soc / soc_step) * soc_step))

    # state -> aggregate record
    states: Dict[float, tuple[float, Dict[str, float]]] = {q_soc(initial_soc): (0.0, {"export":0.0,"import":0.0,"charge":0.0,"discharge":0.0,"curtail":0.0})}
    for interval in intervals:
        nxt: Dict[float, tuple[float, Dict[str, float]]] = {}
        for soc, (score, agg) in states.items():
            stored = capacity * soc / 100.0
            for action in actions:
                new_stored, f = _step_policy(stored, interval, action, raw)
                new_soc = q_soc(new_stored / capacity * 100.0)
                if objective == "economic":
                    econ = raw.get("economic", {})
                    reward = (
                        f["export_kwh"] * float(econ.get("export_eur_kwh", 0.0))
                        - f["import_kwh"] * float(econ.get("import_eur_kwh", 0.0))
                        - f["discharge_output_kwh"] * float(econ.get("battery_wear_eur_kwh", 0.0))
                    )
                else:
                    reward = f["export_kwh"] - f["import_kwh"]
                new_score = score + reward
                old = nxt.get(new_soc)
                if old is None or new_score > old[0] + 1e-12:
                    na = {
                        "export": agg["export"] + f["export_kwh"],
                        "import": agg["import"] + f["import_kwh"],
                        "charge": agg["charge"] + f["charge_input_kwh"],
                        "discharge": agg["discharge"] + f["discharge_output_kwh"],
                        "curtail": agg["curtail"] + f["curtailed_kwh"],
                    }
                    nxt[new_soc] = (new_score, na)
        states = nxt
        if not states:
            break
    feasible_states = [(soc, v) for soc, v in states.items() if soc + 1e-9 >= target]
    feasible = bool(feasible_states)
    if feasible_states:
        soc, (_, agg) = max(feasible_states, key=lambda x: (x[1][0], x[0]))
    elif states:
        # If target is impossible, pick the highest achievable final SOC, then best grid score.
        max_soc = max(states)
        soc, (_, agg) = max(((s, v) for s, v in states.items() if abs(s-max_soc)<1e-9), key=lambda x: x[1][0])
    else:
        return SimResult(label="Oracle", cap_w=hard, final_soc_pct=initial_soc, feasible=False)
    throughput = agg["charge"] + agg["discharge"]
    result = SimResult(
        label=("Oracle € (perfect foresight)" if objective == "economic" else "Oracle (perfect foresight)"), cap_w=hard, export_kwh=agg["export"], import_kwh=agg["import"],
        charge_input_kwh=agg["charge"], discharge_output_kwh=agg["discharge"], curtailed_kwh=agg["curtail"],
        final_soc_pct=soc, min_soc_pct=floor, max_soc_pct=ceiling, battery_throughput_kwh=throughput,
        equivalent_full_cycles=throughput/max(0.001,2*capacity), feasible=feasible,
    )
    result.economic_cost_eur = _economic_cost(raw, result.export_kwh, result.import_kwh, result.discharge_output_kwh)
    return result


def _forecast_rows(db_path: str, date: dt.date) -> list[Dict[str, Any]]:
    c = _conn(db_path)
    try:
        rows = c.execute("SELECT * FROM forecasts WHERE target_date=? ORDER BY fetched_at", (date.isoformat(),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def forecast_vs_actual(raw: Dict[str, Any], db_path: str, date: dt.date) -> Dict[str, Any]:
    rows = _forecast_rows(db_path, date)
    intervals, _ = day_intervals(db_path, date)
    actual_cum = []
    total = 0.0
    for i in intervals:
        total += i.pv_w / 1000.0 * i.hours
        actual_cum.append({"time": i.end.isoformat(), "kwh": round(total, 4)})
    revisions = [{"fetched_at": r["fetched_at"], "expected_kwh": r["expected_kwh"], "lead_bucket": r.get("lead_bucket")} for r in rows]
    forecast_series = []
    for name, r in (("earliest", rows[0] if rows else None), ("latest", rows[-1] if rows else None)):
        if not r or not r.get("points_json"):
            continue
        try:
            pts = json.loads(r["points_json"])
        except Exception:
            continue
        cum, seq = 0.0, []
        for p in pts:
            cum += max(0.0, float(p.get("predicted_w",0.0))) / 1000.0 * 0.25
            seq.append({"time": p["time"], "kwh": round(cum,4)})
        forecast_series.append({"name":name,"fetched_at":r["fetched_at"],"points":seq})
    tz = ZoneInfo(raw["site"]["timezone"])
    db = StateDB(raw["logging"]["state_db"], readonly=True)
    try:
        dist, source = forecast_distribution(raw, db, dt.datetime.now(tz), date)
    finally:
        db.close()
    return {"date": date.isoformat(), "revisions": revisions, "actual_cumulative": actual_cum, "forecast_series": forecast_series, "probabilistic_factors": dist, "probabilistic_source": source}


def battery_aging(raw: Dict[str, Any], intervals: list[Interval], actual: Dict[str, Any]) -> Dict[str, Any]:
    if not intervals:
        return {}
    cap = float(raw["battery"]["effective_kwh"])
    weighted_soc, hours = 0.0, 0.0
    above95 = below20 = 0.0
    c_rates = []
    for i in intervals:
        if i.soc_pct is not None:
            weighted_soc += i.soc_pct * i.hours
            hours += i.hours
            if i.soc_pct >= 95: above95 += i.hours
            if i.soc_pct <= 20: below20 += i.hours
        if i.battery_w is not None and cap > 0:
            c_rates.append(abs(i.battery_w)/1000.0/cap)
    return {
        "throughput_kwh": actual.get("battery_throughput_kwh", 0.0),
        "equivalent_full_cycles": actual.get("equivalent_full_cycles", 0.0),
        "hours_soc_ge_95": round(above95,3), "hours_soc_le_20": round(below20,3),
        "average_soc_pct": round(weighted_soc/hours,2) if hours else None,
        "average_c_rate": round(statistics.fmean(c_rates),4) if c_rates else None,
        "max_c_rate": round(max(c_rates),4) if c_rates else None,
    }


def _successful_setting_timeline(db_path: str, date: dt.date, hard: int) -> list[tuple[dt.datetime,int]]:
    c=_conn(db_path)
    try:
        before=c.execute("SELECT ts,requested_w FROM writes WHERE status='success' AND accepted=1 AND ts<? ORDER BY ts DESC LIMIT 1",(date.isoformat()+"T00:00:00",)).fetchone()
        rows=c.execute("SELECT ts,requested_w FROM writes WHERE status='success' AND accepted=1 AND substr(ts,1,10)=? ORDER BY ts",(date.isoformat(),)).fetchall()
    finally: c.close()
    out=[]
    if before:
        out.append((dt.datetime.fromisoformat(before["ts"]), int(before["requested_w"])))
    else:
        out.append((dt.datetime.combine(date,dt.time.min,tzinfo=ZoneInfo("UTC")),hard))
    for r in rows:
        try: out.append((dt.datetime.fromisoformat(r["ts"]),int(r["requested_w"])))
        except Exception: pass
    return out


def _nearest_forecast_w(fp: list, when: dt.datetime) -> float:
    return min(fp, key=lambda x: abs((x[0] - when).total_seconds()))[1] if fp else 0.0


def _self_calibration_ratio(fp: list, intervals: list[Interval], soc_thr: float,
                            absorb_w: float) -> tuple[Optional[float], str, int]:
    """Scale the forecast by how the array performed while it was demonstrably unclipped.

    A cross-day multiplier cannot separate a pessimistic forecast from a clipped
    afternoon, and the two look identical in the PV trace: measured output falls in
    both cases. The day's own pre-clipping intervals settle it. Whatever ratio the
    array was achieving while the battery could still absorb is the best available
    estimate of what it would have achieved once it could not.
    """
    num = den = 0.0
    n = 0
    for it in intervals:
        if it.soc_pct is None or it.soc_pct >= soc_thr:
            continue
        # Counter-reconstructed intervals carry an instantaneous power that is an
        # artifact of sample spacing, not a measurement. Calibrating on those lets a
        # single reconstruction spike scale the whole day's counterfactual.
        if it.synthesized_from_counters:
            continue
        charge_w = max(0.0, -(it.battery_w or 0.0))
        # Require headroom actually being used, so "unclipped" is observed, not assumed.
        if charge_w < absorb_w:
            continue
        pred = _nearest_forecast_w(fp, it.start)
        if pred <= 200.0:
            continue
        num += it.pv_w * it.hours
        den += pred * it.hours
        n += 1
    if n < 6 or den <= 0:
        return None, "insufficient_unclipped_intervals", n
    ratio = num / den
    # Beyond this band the forecast model itself is wrong rather than the array
    # being clipped, and scaling a counterfactual by it produces nonsense.
    if not (0.5 <= ratio <= 1.6):
        return None, f"calibration_ratio_{ratio:.2f}_outside_plausible_band", n
    return ratio, f"self_calibrated_on_{n}_unclipped_intervals", n


def curtailment_estimate(raw: Dict[str, Any], db_path: str, date: dt.date, intervals: list[Interval]) -> Dict[str, Any]:
    rows=_forecast_rows(db_path,date)
    rows=[r for r in rows if r.get("points_json")]
    if not rows or not intervals:
        return {"estimated_kwh":0.0,"status":"waiting_for_v3.1_forecast_points"}
    r=rows[-1]
    try:
        pts=json.loads(r["points_json"])
        fp=[(dt.datetime.fromisoformat(p["time"]),float(p.get("predicted_w",0.0))) for p in pts]
    except Exception:
        return {"estimated_kwh":0.0,"status":"invalid_forecast_points"}
    # Use P50 learned multiplier if mature; otherwise 1.0: estimate is deliberately labelled rough.
    tz=ZoneInfo(raw["site"]["timezone"]); db=StateDB(db_path, readonly=True)
    try: dist,_=forecast_distribution(raw,db,dt.datetime.now(tz),date)
    finally: db.close()
    learned=dist.get("p50",1.0)
    hard=int(raw["grid"]["export_hard_limit_w"])
    timeline=_successful_setting_timeline(db_path,date,hard)
    soc_thr=float(raw["analytics"].get("curtailment_soc_threshold_pct",98.0))
    margin=float(raw["analytics"].get("curtailment_export_margin_w",100.0))
    min_gap=float(raw["analytics"].get("curtailment_min_gap_w",300.0))
    absorb_w=float(raw["analytics"].get("curtailment_max_charge_w",300.0))
    installed_w=sum(float(a.get("kwp",0.0)) for a in raw.get("pv",{}).get("arrays",[]))*1000.0
    array_ceiling_w=float(raw["analytics"].get("curtailment_array_ceiling_w", 0.0)) or (installed_w or 1e9)
    calib,calib_src,calib_n=_self_calibration_ratio(fp,intervals,soc_thr,absorb_w)
    factor = calib if calib is not None else learned
    if calib is None:
        calib_src=f"{calib_src}_fell_back_to_learned_p50"
    total=0.0; flagged=0; absorbing_skipped=0; synth_skipped=0
    measured_kwh=sum(it.pv_w/1000.0*it.hours for it in intervals)
    for it in intervals:
        if it.soc_pct is None or it.soc_pct<soc_thr or it.grid_w is None:
            continue
        # A battery still taking charge is a sink: whatever the PV trace shows, the
        # surplus had somewhere to go and was not curtailed.
        if max(0.0,-(it.battery_w or 0.0)) > absorb_w:
            absorbing_skipped+=1
            continue
        setting=hard
        for t,w in timeline:
            try:
                tt=t.astimezone(it.start.tzinfo) if t.tzinfo and it.start.tzinfo else t.replace(tzinfo=it.start.tzinfo)
            except Exception: tt=t
            if tt<=it.start: setting=w
        actual_export=max(0.0,-it.grid_w)
        if actual_export < max(0.0,setting-margin):
            continue
        if it.synthesized_from_counters:
            absorbing_skipped+=0   # counted separately below
            synth_skipped+=1
            continue
        # The counterfactual cannot exceed what the array is physically able to make.
        # Without this a mis-calibrated forecast scales into an impossible potential.
        pred=min(_nearest_forecast_w(fp,it.start)*factor, array_ceiling_w)
        gap=max(0.0,pred-it.pv_w)
        if gap>=min_gap:
            total += gap/1000.0*it.hours; flagged+=1
    return {
        "estimated_kwh":round(total,3),
        "flagged_intervals":flagged,
        "intervals_skipped_battery_absorbing":absorbing_skipped,
        "intervals_skipped_reconstructed":synth_skipped,
        "status":"model_estimate_self_calibrated" if calib is not None else "model_estimate_uncalibrated",
        "forecast_factor":round(factor,3),
        "calibration":calib_src,
        "learned_p50":round(learned,3),
        "array_ceiling_w":round(array_ceiling_w),
        "implied_potential_kwh":round(measured_kwh+total,2),
        "implied_kwh_per_kwp":round((measured_kwh+total)/(installed_w/1000.0),2) if installed_w else None,
        # The estimate rests on scaling a forecast that may itself be miscalibrated,
        # so it states the daily yield it implies. A value far above what the array
        # can plausibly reach at this latitude and season means the factor is wrong,
        # not that this much energy was lost.
        "plausible": (None if not installed_w
                      else (measured_kwh+total)/(installed_w/1000.0) <= float(
                          raw["analytics"].get("curtailment_max_kwh_per_kwp", 3.6))),
    }


def mppt_learning(raw: Dict[str, Any], db_path: str, date: dt.date, intervals: list[Interval]) -> Dict[str, Any]:
    energies: Dict[str,float]={}
    for it in intervals:
        for k,w in it.mppt_w.items(): energies[k]=energies.get(k,0.0)+w/1000.0*it.hours
    mapping=raw.get("analytics",{}).get("mppt_map",{}) or {}
    rows=_forecast_rows(db_path,date)
    fc_arrays={}
    if rows:
        try: fc_arrays=json.loads(rows[0].get("array_kwh_json") or "{}")
        except Exception: pass
    comparisons=[]
    for mppt,kwh in sorted(energies.items()):
        array=mapping.get(mppt)
        forecast=_f(fc_arrays.get(array)) if array else None
        comparisons.append({"mppt":mppt,"array":array,"observed_kwh":round(kwh,3),"forecast_kwh":forecast,"ratio":round(kwh/forecast,3) if forecast and forecast>0 else None})
    return {"date":date.isoformat(),"mapping":mapping,"channels":comparisons,"note":"MPPT energy is trapezoid/interval integrated; long cloud gaps reduce timing confidence."}


def morning_learning_summary(raw: Dict[str, Any], db_path: str, before_date: dt.date) -> Dict[str, Any]:
    db=StateDB(db_path, readonly=True)
    try:
        ml=raw.get("morning_learning",{}); ns=raw.get("night_strategy",{})
        errs=db.morning_timing_errors(before_date,int(ml.get("learning_days",30)),float(ns.get("morning_surplus_threshold_w",350)),int(ns.get("sustained_minutes",30)))
    finally: db.close()
    min_days=int(raw.get("morning_learning",{}).get("min_days",7))
    if not errs:
        return {"samples":0,"active":False,"status":f"waiting 0/{min_days} post-v3.1 days"}
    med=statistics.median(errs); limit=abs(float(raw.get("morning_learning",{}).get("max_abs_minutes",45)))
    return {"samples":len(errs),"active":len(errs)>=min_days,"median_error_minutes":round(med,1),"applied_bias_minutes":round(max(-limit,min(limit,med)),1) if len(errs)>=min_days else 0.0,"errors_minutes":[round(x,1) for x in errs],"status":"active" if len(errs)>=min_days else f"waiting {len(errs)}/{min_days} post-v3.1 days"}


def day_timeline(raw: Dict[str, Any], db_path: str, date: dt.date, intervals: list[Interval]) -> Dict[str, Any]:
    """Intraday power/SOC series plus the confirmed export-cap steps.

    This is the view that makes curtailment visible: SOC pinned at the ceiling while
    the export setpoint sits below the hard limit and PV is still producing.
    """
    hard = int(raw["grid"]["export_hard_limit_w"])
    tz = ZoneInfo(raw["site"]["timezone"])
    points = []
    for iv in intervals:
        grid_w = iv.grid_w
        points.append({
            "time": iv.start.astimezone(tz).isoformat(),
            "pv_w": round(iv.pv_w, 1),
            "load_w": round(iv.load_w, 1),
            "soc_pct": None if iv.soc_pct is None else round(iv.soc_pct, 1),
            "battery_w": None if iv.battery_w is None else round(iv.battery_w, 1),
            "export_w": None if grid_w is None else round(max(0.0, -grid_w), 1),
            "import_w": None if grid_w is None else round(max(0.0, grid_w), 1),
            "synthesized": bool(iv.synthesized_from_counters),
        })
    steps = []
    for ts, watts in _successful_setting_timeline(db_path, date, hard):
        steps.append({"time": ts.astimezone(tz).isoformat(), "setting_w": int(watts)})
    return {"hard_limit_w": hard, "points": points, "setting_steps": steps}


# How a decision's recorded action maps onto "could the controller write right now".
GATE_LABELS = {
    "open": "Open — free to write",
    "writing": "Write in flight",
    "blocked_cooldown": "Blocked — write cooldown or budget",
    "blocked_backoff": "Blocked — failed-order backoff",
    "blocked_cloud": "Blocked — DeyeCloud stale or refusing",
    "no_data": "No controller data",
}


def classify_gate(action: str) -> str:
    a = (action or "").strip()
    if a in ("direct_offline", "direct_busy", "waiting_control_retry"):
        return "blocked_cloud"
    if a.startswith("blocked_cloud") or a.startswith("blocked_telemetry"):
        return "blocked_cloud"
    if a.startswith("blocked_failed-order"):
        return "blocked_backoff"
    if a.startswith("blocked_"):
        return "blocked_cooldown"
    if a.startswith("direct_accepted") or a.startswith("order_"):
        return "writing"
    return "open"


def _deye_error_code(details: str | None) -> Optional[str]:
    try:
        d = json.loads(details or "")
    except Exception:
        return None
    code = d.get("error")
    return str(code) if code not in (None, "") else None


def control_log(raw: Dict[str, Any], db_path: str, date: dt.date) -> Dict[str, Any]:
    """Write attempts and reconstructed write-window timings for one local day.

    The controller records an action on every control tick, so contiguous runs of
    the same action are exactly the intervals during which a write was or was not
    possible. Gaps longer than the tick interval mean the controller was not running.
    """
    tz = ZoneInfo(raw["site"]["timezone"])
    day_start = dt.datetime.combine(date, dt.time.min, tzinfo=tz)
    day_end = day_start + dt.timedelta(days=1)
    now = dt.datetime.now(tz)
    horizon = min(day_end, now) if day_start <= now else day_end
    tick_s = int(raw.get("control", {}).get("loop_seconds", 60))
    gap_limit = dt.timedelta(seconds=max(300, tick_s * 5))

    c = _conn(db_path)
    try:
        drows = c.execute(
            "SELECT ts,action,reason,recommended_w,current_setting_w FROM decisions "
            "WHERE substr(ts,1,10)=? ORDER BY ts", (date.isoformat(),)
        ).fetchall()
        wrows = c.execute(
            "SELECT ts,previous_w,requested_w,reason,order_id,status,accepted,updated_at,details "
            "FROM writes WHERE substr(ts,1,10)=? ORDER BY ts", (date.isoformat(),)
        ).fetchall()
        last_ok = c.execute(
            "SELECT ts FROM writes WHERE status='success' AND accepted=1 ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    finally:
        c.close()

    parsed = []
    for r in drows:
        try:
            parsed.append((dt.datetime.fromisoformat(r["ts"]), r))
        except Exception:
            continue

    segments: list[Dict[str, Any]] = []

    def push(start, end, state, detail):
        if end <= start:
            return
        if segments and segments[-1]["state"] == state and segments[-1]["detail"] == detail:
            segments[-1]["end"] = end.isoformat()
            segments[-1]["minutes"] = round(
                (end - dt.datetime.fromisoformat(segments[-1]["start"])).total_seconds() / 60.0, 1)
            return
        segments.append({
            "start": start.isoformat(), "end": end.isoformat(), "state": state,
            "label": GATE_LABELS.get(state, state), "detail": detail,
            "minutes": round((end - start).total_seconds() / 60.0, 1),
        })

    for i, (ts, r) in enumerate(parsed):
        nxt = parsed[i + 1][0] if i + 1 < len(parsed) else horizon
        end = min(nxt, horizon)
        if nxt - ts > gap_limit:
            end = min(ts + dt.timedelta(seconds=tick_s), horizon)
        push(ts, end, classify_gate(r["action"]), str(r["action"] or ""))
        if nxt - ts > gap_limit and end < min(nxt, horizon):
            push(end, min(nxt, horizon), "no_data", "controller not reporting")
    if parsed and parsed[0][0] > day_start:
        segments.insert(0, {
            "start": day_start.isoformat(), "end": parsed[0][0].isoformat(), "state": "no_data",
            "label": GATE_LABELS["no_data"], "detail": "controller not reporting",
            "minutes": round((parsed[0][0] - day_start).total_seconds() / 60.0, 1),
        })

    summary: Dict[str, float] = {}
    for seg in segments:
        summary[seg["state"]] = round(summary.get(seg["state"], 0.0) + seg["minutes"], 1)

    writes = []
    for r in wrows:
        writes.append({
            "time": r["ts"], "confirmed_at": r["updated_at"],
            "previous_w": r["previous_w"], "requested_w": r["requested_w"],
            "delta_w": None if r["previous_w"] is None else int(r["requested_w"]) - int(r["previous_w"]),
            "reason": r["reason"], "order_id": r["order_id"], "status": r["status"],
            "accepted": bool(r["accepted"]), "error_code": _deye_error_code(r["details"]),
        })

    # When the next window can open, as far as the recorded state can say.
    gates = []
    retry = None
    c = _conn(db_path)
    try:
        row = c.execute("SELECT value FROM kv WHERE key='failed_order_retry_after'").fetchone()
        retry = json.loads(row["value"]) if row else None
    except Exception:
        retry = None
    finally:
        c.close()
    if isinstance(retry, str):
        try:
            rt = dt.datetime.fromisoformat(retry)
            if rt > now:
                gates.append({"gate": "failed-order backoff", "until": rt.isoformat()})
        except Exception:
            pass
    cooldown_min = int(raw.get("control", {}).get("min_write_interval_minutes", 120))
    if last_ok:
        try:
            until = dt.datetime.fromisoformat(last_ok["ts"]) + dt.timedelta(minutes=cooldown_min)
            if until > now:
                gates.append({"gate": f"successful-write cooldown ({cooldown_min} min)", "until": until.isoformat()})
        except Exception:
            pass

    current = None
    if segments and day_start <= now < day_end:
        last = segments[-1]
        if dt.datetime.fromisoformat(last["end"]) >= now - dt.timedelta(minutes=5):
            current = {"state": last["state"], "label": last["label"], "detail": last["detail"],
                       "since": last["start"], "minutes": last["minutes"]}

    return {
        "date": date.isoformat(), "timezone": raw["site"]["timezone"],
        "segments": segments, "summary_minutes": summary, "writes": writes,
        "current": current, "open_blocking_gates": gates,
        "next_window_not_before": max([g["until"] for g in gates], default=None),
        "successful_writes": sum(1 for w in writes if w["status"] == "success"),
        "failed_writes": sum(1 for w in writes if w["status"] == "failed"),
        "submissions": sum(1 for w in writes if w["accepted"]),
    }


def _forecast_day_object(raw: Dict[str, Any], db_path: str, date: dt.date):
    """Rebuild a forecast day from stored rows, for reuse of the strategy helpers."""
    rows = _forecast_rows(db_path, date)
    if not rows:
        return None
    r = rows[-1]
    try:
        pts = [SimpleNamespace(time=dt.datetime.fromisoformat(p["time"]),
                               predicted_w=float(p["predicted_w"]))
               for p in json.loads(r["points_json"] or "[]")]
        return SimpleNamespace(
            date=date,
            sunrise=dt.datetime.fromisoformat(r["sunrise"]),
            sunset=dt.datetime.fromisoformat(r["sunset"]),
            pv_wakeup=dt.datetime.fromisoformat(r["pv_wakeup"]),
            useful_pv_start=dt.datetime.fromisoformat(r["useful_pv_start"]),
            points=pts,
        )
    except Exception:
        return None


def oracle_end_of_day_target(raw: Dict[str, Any], db_path: str, date: dt.date) -> tuple[float, str]:
    """The end-of-day SOC the optimizer was actually aiming at on that date.

    Holding the oracle to a fixed day target while the live plan aims at the night's
    real need flatters the optimizer: the benchmark is forbidden from spending
    battery the optimizer was free to spend, so a day looks closer to optimal than
    it was. This mirrors the strategy's own requirement instead.
    """
    default = float(raw["battery"].get("day_target_soc_pct", 96.0))
    day = _forecast_day_object(raw, db_path, date)
    if day is None:
        return default, "no_forecast_stored"
    tomorrow = _forecast_day_object(raw, db_path, date + dt.timedelta(days=1))
    try:
        from energy_strategy import (night_energy_need_kwh, strategy_reserve_kwh,
                                     tomorrow_refill_possible)
        from state_db import StateDB
        from strategy_presets import active_strategy
        policy = active_strategy(raw)
        db = StateDB(db_path, readonly=True)
        try:
            now = day.sunset
            need, _hours = night_energy_need_kwh(raw, db, day, tomorrow, now)
            reserve, _src = strategy_reserve_kwh(raw, db, now, policy)
            capacity = float(raw["battery"]["effective_kwh"])
            floor = float(raw["battery"]["soc_floor_pct"])
            required = floor + (need + reserve) / capacity * 100.0 if capacity > 0 else default
            required = max(floor, min(100.0, required))
            if tomorrow_refill_possible(raw, db, tomorrow, now, required, default):
                return required, "night_need_plus_reserve"
            return max(required, default), "day_target_held_forecast_cannot_refill"
        finally:
            db.close()
    except Exception:
        return default, "fallback_day_target"


def day_report(raw: Dict[str, Any], date: dt.date, custom_cap_w: Optional[int] = None) -> Dict[str, Any]:
    db_path=raw["logging"]["state_db"]
    intervals,quality=day_intervals(db_path,date)
    actual=actual_day_metrics(raw,db_path,date,intervals)
    caps=list(raw.get("analytics",{}).get("replay_caps_w",[0,300,500,800,1000]))
    if custom_cap_w is not None and int(custom_cap_w) not in caps: caps.append(int(custom_cap_w))
    caps=sorted(set(max(0,min(int(raw["grid"]["export_hard_limit_w"]),int(x))) for x in caps))
    sims=[simulate_static(raw,intervals,c).as_dict() for c in caps] if intervals else []
    oracle_target,oracle_target_reason=oracle_end_of_day_target(raw,db_path,date)
    oracle=oracle_replay(raw,intervals,oracle_target, objective="energy").as_dict() if intervals else {}
    economic_oracle=oracle_replay(raw,intervals,oracle_target, objective="economic").as_dict() if intervals else {}
    for o in (oracle,economic_oracle):
        if o:
            o["end_of_day_target_soc_pct"]=round(oracle_target,2)
            o["end_of_day_target_reason"]=oracle_target_reason
    for s in sims:
        if actual:
            s["optimizer_export_delta_kwh"] = round(actual.get("export_kwh",0.0)-s["export_kwh"],3)
            s["optimizer_import_delta_kwh"] = round(actual.get("import_kwh",0.0)-s["import_kwh"],3)
            s["optimizer_cost_delta_eur"] = round(actual.get("economic_cost_eur",0.0)-s["economic_cost_eur"],3)
    return {
        "date":date.isoformat(),"quality":quality,"actual":actual,"counterfactuals":sims,"oracle":oracle,"economic_oracle":economic_oracle,
        "forecast":forecast_vs_actual(raw,db_path,date),"battery_aging":battery_aging(raw,intervals,actual),
        "curtailment":curtailment_estimate(raw,db_path,date,intervals),"mppt":mppt_learning(raw,db_path,date,intervals),
        "timeline":day_timeline(raw,db_path,date,intervals),
        "morning_learning":morning_learning_summary(raw,db_path,date+dt.timedelta(days=1)),
        "economics":raw.get("economic",{}),
        "notes":[
            "Counterfactuals replay observed/energy-counter-reconstructed PV and load; estimated gains are lower-confidence across long telemetry gaps.",
            "Curtailment is a rough forecast-model estimate, not a revenue-grade irradiance measurement.",
            "Oracle has perfect hindsight and is a benchmark, not a live control policy.",
            "The oracle is held to the same end-of-day SOC requirement as the live plan, not a fixed day target.",
        ],
    }


def history_summary(raw: Dict[str, Any], days: int) -> Dict[str, Any]:
    tz=ZoneInfo(raw["site"]["timezone"]); today=dt.datetime.now(tz).date()
    reports=[]
    for n in range(max(1,int(days))):
        d=today-dt.timedelta(days=n)
        intervals,q=day_intervals(raw["logging"]["state_db"],d)
        if not intervals: continue
        a=actual_day_metrics(raw,raw["logging"]["state_db"],d,intervals)
        static=simulate_static(raw,intervals,int(raw["grid"]["export_hard_limit_w"])).as_dict()
        reports.append({"date":d.isoformat(),"actual":a,"static_hard_limit":static,"quality":q})
    if not reports: return {"days":0,"items":[]}
    def sm(path): return sum(float(r[path[0]].get(path[1],0.0) or 0.0) for r in reports)
    return {"days":len(reports),"items":reports,
        "totals":{"actual_export_kwh":round(sm(("actual","export_kwh")),3),"actual_import_kwh":round(sm(("actual","import_kwh")),3),"static_export_kwh":round(sm(("static_hard_limit","export_kwh")),3),"static_import_kwh":round(sm(("static_hard_limit","import_kwh")),3),"actual_cost_eur":round(sm(("actual","economic_cost_eur")),3),"static_cost_eur":round(sm(("static_hard_limit","economic_cost_eur")),3)}}
