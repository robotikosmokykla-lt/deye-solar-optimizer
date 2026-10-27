#!/usr/bin/env python3
"""Analytics dashboard with optional, brokered occupied/away mode control."""
from __future__ import annotations
import argparse, datetime as dt, json, mimetypes, os, secrets, socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from analytics_engine import control_log, day_report, history_summary
from config_loader import DEFAULT_ENV, load_config
from state_db import StateDB
from dashboard_modes import broker_request, mode_status, MODES

ROOT = Path(__file__).resolve().parent


def latest_live(raw):
    """Live telemetry plus the control state that explains what the optimizer is doing.

    The decision, the confirmed setpoint and the remaining write budget were already
    in the database but were never exposed, so a stuck export cap was invisible here.
    """
    db=StateDB(raw['logging']['state_db'], readonly=True)
    tz=ZoneInfo(raw['site']['timezone'])
    try:
        r=db.latest_telemetry(); d=db.latest_decision(); f=db.latest_forecast()
        if not r: return {}
        now=dt.datetime.now(tz); midnight=now.replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
        live_age_min=None
        try:
            live_age_min=max(0.0,(now-dt.datetime.fromisoformat(r['logger_at'])).total_seconds()/60.0)
        except Exception:
            live_age_min=r['telemetry_age_minutes']
        hard=int(raw['grid']['export_hard_limit_w'])
        setting=db.get('last_known_setting_w')
        grid_w=r['grid_power']
        export_w=None if grid_w is None else max(0.0,-float(grid_w))
        battery_w=r['battery_power']
        charge_w=None if battery_w is None else max(0.0,-float(battery_w))
        soc=r['soc']
        ds=raw.get('day_strategy',{})
        curtailing=bool(
            soc is not None and charge_w is not None and r['generation_power'] is not None
            and float(soc)>=float(ds.get('curtailment_override_soc_pct',98.0))
            and charge_w<=float(ds.get('curtailment_override_charge_w',300.0))
            and float(r['generation_power'])>=float(ds.get('curtailment_override_min_pv_w',100.0))
            and setting is not None and int(setting)<hard
        )
        try: plan=db.get('day_energy_plan')
        except Exception: plan=None
        return {
            'now':now.isoformat(),'timezone':raw['site']['timezone'],
            'logger_at':r['logger_at'],'observed_at':r['observed_at'],'soc':soc,'control_soc':r['control_soc'],
            'pv_w':r['generation_power'],'load_w':r['consumption_power'],'grid_w':grid_w,'battery_w':battery_w,
            'export_w':export_w,'charge_w':charge_w,
            # Age NOW, recomputed against the clock. The stored column is the age at
            # the moment the sample was ingested and stays frozen while the cloud is
            # dark, which reads as "fresh" during an outage.
            'telemetry_age_minutes':live_age_min,
            'telemetry_age_at_ingest_minutes':r['telemetry_age_minutes'],
            'device_state':r['device_state'],
            'daily_pv_kwh':r['daily_production_kwh'],'daily_load_kwh':r['daily_consumption_kwh'],
            'decision':dict(d) if d else None,'forecast':dict(f) if f else None,
            'control':{
                'hard_limit_w':hard,
                'current_setting_w':None if setting is None else int(setting),
                'recommended_w':db.get('recommended_w'),
                'export_headroom_w':None if (setting is None) else max(0,hard-int(setting)),
                'phase':db.get('phase'),
                'strategy':db.get('active_strategy'),
                'telemetry_health':db.get('telemetry_health_state'),
                'failed_order_retry_after':db.get('failed_order_retry_after'),
                'successful_writes_today':db.successful_writes_since(midnight),
                'max_successful_writes_per_day':int(raw.get('control',{}).get('max_successful_writes_per_day',4)),
                'order_submissions_today':db.order_submissions_since(midnight),
                'max_order_submissions_per_day':int(raw.get('control',{}).get('max_order_submissions_per_day',8)),
                'curtailment_suspected':curtailing,
            },
            'day_plan':plan if isinstance(plan,dict) else None,
        }
    finally: db.close()


class Handler(BaseHTTPRequestHandler):
    server_version='DeyeAnalytics/3.1'
    def log_message(self, fmt,*args):
        print('%s - %s' % (self.address_string(),fmt%args))
    def send_json(self,obj,status=200):
        data=json.dumps(obj,ensure_ascii=False,separators=(',',':')).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def raw_config(self):
        return load_config(self.server.env_path) if getattr(self.server, 'env_path', None) else self.server.raw
    def mode_info(self, raw):
        info = mode_status(raw)
        info['csrf_token'] = self.server.csrf_token
        info['available'] = False
        if not raw.get('analytics', {}).get('mode_control_enabled', False):
            info['message'] = 'Mode switching is not enabled on this dashboard.'
        elif raw.get('control', {}).get('dry_run', True):
            info['message'] = 'Mode switching is unavailable during dry-run.'
        elif not raw.get('charge_temperature', {}).get('enabled'):
            info['message'] = 'Temperature charging protection must be enabled before switching modes.'
        else:
            try:
                remote = broker_request({'op': 'status'})
                if remote.get('error'):raise RuntimeError(remote['error'])
                info.update(remote)
                info['available'] = True
            except (OSError, ValueError, RuntimeError):
                info['message'] = 'Mode controls are temporarily unavailable.'
        return info
    def trusted_write_request(self, raw):
        host = self.headers.get('Host', '')
        hostname = urlparse('//' + host).hostname
        allowed = {'localhost', '127.0.0.1', '::1', socket.gethostname().lower(), socket.getfqdn().lower()}
        allowed.update(raw.get('analytics', {}).get('allowed_hosts', []))
        allowed.add(str(self.server.server_address[0]).lower())
        if not hostname or hostname.lower() not in allowed:
            return False
        origin = self.headers.get('Origin')
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in {'http', 'https'} or parsed.netloc.lower() != host.lower():return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':return False
        token = self.headers.get('X-CSRF-Token', '')
        return bool(token) and secrets.compare_digest(token, self.server.csrf_token)
    def do_POST(self):
        if urlparse(self.path).path != '/api/mode':return self.send_json({'error':'not found'},404)
        try:
            raw = self.raw_config()
            if not self.trusted_write_request(raw):
                return self.send_json({'error':'Mode request rejected. Refresh this dashboard and retry.'},403)
            if self.headers.get_content_type() != 'application/json':
                return self.send_json({'error':'Expected application/json'},415)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 256:return self.send_json({'error':'Invalid request size'},400)
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict) or set(request) != {'mode'} or request['mode'] not in MODES:
                return self.send_json({'error':'Choose occupied or winter-away'},400)
            info = self.mode_info(raw)
            if not info['available']:return self.send_json({'error':info['message']},503)
            result = broker_request({'op':'switch', 'mode':request['mode']})
            return self.send_json(result, int(result.get('status',200)))
        except (ValueError, TypeError):
            return self.send_json({'error':'Invalid mode request'},400)
        except Exception:
            return self.send_json({'error':'Mode request could not be confirmed. Refresh the current mode before retrying.'},503)
    def do_GET(self):
        u=urlparse(self.path); q=parse_qs(u.query)
        try:
            raw=self.raw_config()
            tz=ZoneInfo(raw['site']['timezone']); today=dt.datetime.now(tz).date()
            if u.path in ('/','/index.html'):
                data=(ROOT/'dashboard.html').read_bytes(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
            if u.path=='/api/live': return self.send_json(latest_live(raw))
            if u.path=='/api/mode': return self.send_json(self.mode_info(raw))
            if u.path=='/api/day':
                date=dt.date.fromisoformat(q.get('date',[today.isoformat()])[0]); cap=q.get('cap',[None])[0]; cap=int(cap) if cap not in (None,'') else None
                return self.send_json(day_report(raw,date,cap))
            if u.path=='/api/control':
                date=dt.date.fromisoformat(q.get('date',[today.isoformat()])[0])
                return self.send_json(control_log(raw,raw['logging']['state_db'],date))
            if u.path=='/api/history':
                days=max(1,min(365,int(q.get('days',[raw.get('analytics',{}).get('history_days',30)])[0])))
                return self.send_json(history_summary(raw,days))
            if u.path=='/api/config':
                return self.send_json({'strategy':raw.get('strategy',{}),'grid':raw.get('grid',{}),'battery':raw.get('battery',{}),'analytics':raw.get('analytics',{}),'economic':raw.get('economic',{}),'site':{'timezone':raw['site']['timezone']}})
            if u.path=='/health': return self.send_json({'ok':True,'version':'3.1.10'})
            return self.send_json({'error':'not found'},404)
        except Exception as e:
            return self.send_json({'error':type(e).__name__,'message':str(e)},500)


def main():
    ap=argparse.ArgumentParser(description='Deye analytics and optional house-mode dashboard')
    ap.add_argument('--env',default=DEFAULT_ENV); ap.add_argument('--bind'); ap.add_argument('--port',type=int)
    args=ap.parse_args(); raw=load_config(args.env)
    if not bool(raw.get('analytics',{}).get('enabled',True)):
        print('Analytics dashboard disabled by ANALYTICS_ENABLED=false')
        return
    bind=args.bind or raw.get('analytics',{}).get('bind','127.0.0.1'); port=args.port or int(raw.get('analytics',{}).get('port',8787))
    httpd=ThreadingHTTPServer((bind,port),Handler); httpd.raw=raw
    httpd.env_path=args.env; httpd.csrf_token=secrets.token_urlsafe(32)
    print(f'Deye analytics dashboard: http://{bind}:{port}/')
    httpd.serve_forever()

if __name__=='__main__': main()
