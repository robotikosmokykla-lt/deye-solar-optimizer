#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, json
from zoneinfo import ZoneInfo
from analytics_engine import day_report, history_summary
from config_loader import DEFAULT_ENV, load_config

def main():
    ap=argparse.ArgumentParser(description='Deye read-only analytics/replay')
    ap.add_argument('--env',default=DEFAULT_ENV); ap.add_argument('--date'); ap.add_argument('--cap',type=int); ap.add_argument('--days',type=int); ap.add_argument('--json',action='store_true')
    a=ap.parse_args(); raw=load_config(a.env); tz=ZoneInfo(raw['site']['timezone']); date=dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(tz).date()
    out=history_summary(raw,a.days) if a.days else day_report(raw,date,a.cap)
    if a.json: print(json.dumps(out,indent=2,ensure_ascii=False)); return
    if a.days:
        print(f"Analytics history: {out.get('days',0)} days"); print(json.dumps(out.get('totals',{}),indent=2)); return
    print(f"Deye analytics {out['date']}  coverage={out['quality'].get('coverage_pct')}%")
    act=out.get('actual',{}); print(f"Actual: export={act.get('export_kwh',0):.2f}kWh import={act.get('import_kwh',0):.2f}kWh PV={act.get('pv_kwh',0):.2f}kWh finalSOC={act.get('final_soc_pct')}")
    for s in out.get('counterfactuals',[]): print(f"{s['label']}: export={s['export_kwh']:.2f} import={s['import_kwh']:.2f} curtail={s['curtailed_kwh']:.2f} finalSOC={s['final_soc_pct']:.1f}% deltaExport={s.get('optimizer_export_delta_kwh',0):+.2f}")
    o=out.get('oracle',{}); print(f"Oracle: export={o.get('export_kwh',0):.2f} import={o.get('import_kwh',0):.2f} finalSOC={o.get('final_soc_pct')} feasible={o.get('feasible')}")
    print('Forecast probabilistic:', out.get('forecast',{}).get('probabilistic_source'), out.get('forecast',{}).get('probabilistic_factors'))
    print('Curtailment estimate:', out.get('curtailment'))
    print('Morning learner:', out.get('morning_learning'))

if __name__=='__main__': main()
