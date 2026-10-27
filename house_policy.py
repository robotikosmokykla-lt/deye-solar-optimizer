"""Occupied/away reserves. Forecasts prepare reserves; measured temperature
alone authorizes charging. Indoor cooling estimates are conservative scenarios,
not predictions of cell temperature or learned unheated-house behaviour.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import urllib.parse
import urllib.request


def state_path(cfg):
    return Path(cfg['logging']['state_db']).parent / 'house-state.json'


def read_state(cfg):
    try:
        return json.loads(state_path(cfg).read_text())
    except FileNotFoundError:
        return {}


def apply_plan(cfg, plan):
    house = cfg.get('house', {})
    if not house.get('enabled'):
        return
    house.setdefault('base_day_soc', cfg['battery']['soc_floor_pct'])
    house.setdefault('base_night_soc', cfg['night_strategy']['morning_soc_pct'])
    mode = house['mode']
    if mode == 'winter-away':
        reserve = house['storage_soc_pct']
    elif plan.get('mode') == mode:
        reserve = max(house['base_night_soc'], float(plan['reserve_soc_pct']))
    else:
        reserve = house['base_night_soc']
    reserve = min(house['storage_soc_pct'], reserve)
    cfg['night_strategy']['morning_soc_pct'] = reserve
    # Preserve the 15% morning-use window unless extra cold reserve is needed.
    cfg['battery']['soc_floor_pct'] = reserve if mode == 'winter-away' or reserve > house['base_night_soc'] else house['base_day_soc']
    cfg['season']['grid_charge_ampere'] = int(plan.get('grid_charge_a', 10)) if plan.get('mode') == mode else 10
    house['plan'] = plan if plan.get('mode') == mode else {'mode': mode, 'reserve_soc_pct': reserve}
    if mode == 'winter-away':
        cfg['night_strategy']['max_export_w'] = 0
        cfg['grid']['day_export_w'] = 0


def fetch_weather(site, now, station=''):
    params = urllib.parse.urlencode({'latitude': site['latitude'], 'longitude': site['longitude'],
                                    'hourly': 'temperature_2m', 'current': 'temperature_2m',
                                    'timezone': 'UTC', 'forecast_days': 3})
    with urllib.request.urlopen('https://api.open-meteo.com/v1/forecast?' + params, timeout=12) as response:
        data = json.load(response)
    weather = {'fetched_at': now.isoformat(), 'current_air_c': float(data['current']['temperature_2m']),
               'source': 'Open-Meteo', 'points': []}
    for text, value in zip(data['hourly']['time'], data['hourly']['temperature_2m']):
        if value is not None and math.isfinite(float(value)):
            weather['points'].append([dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).isoformat(), float(value)])
    if station:
        # Observations anchor the forecast drop; a failed station read falls back
        # to the forecast model's current air temperature.
        try:
            station = urllib.parse.quote(station, safe='')
            with urllib.request.urlopen(f'https://api.meteo.lt/v1/stations/{station}/observations/latest', timeout=8) as response:
                observations = json.load(response)['observations']
            valid = [r for r in observations if r.get('airTemperature') is not None
                     and 0 <= (now - dt.datetime.fromisoformat(r['observationTimeUtc']).replace(tzinfo=dt.timezone.utc)).total_seconds() <= 10800]
            if valid:
                latest = max(valid, key=lambda r: r['observationTimeUtc'])
                weather.update(current_air_c=float(latest['airTemperature']), source=f'LHMT {station} + Open-Meteo')
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return weather


def battery_history(db, now):
    from deye_api import flatten_device_latest
    unique = {}
    for row in db.conn.execute('SELECT logger_at,raw_json,battery_power FROM telemetry ORDER BY id DESC LIMIT 400'):
        try:
            timestamp = dt.datetime.fromisoformat(row['logger_at'])
            age = (now - timestamp).total_seconds() / 3600
            temperature = float(flatten_device_latest(json.loads(row['raw_json']))['metrics']['Temperature- Battery'])
            if 0 <= age <= 12 and math.isfinite(temperature) and -50 <= temperature <= 100:
                unique.setdefault(timestamp, (temperature, abs(row['battery_power'] or 0)))
        except (ValueError, TypeError, KeyError):
            continue
    return sorted((t, v[0], v[1]) for t, v in unique.items())


def reserve_for_temperature(temperature):
    if temperature >= 15:return 20
    if temperature >= 12:return 30
    if temperature >= 10:return 40
    if temperature >= 7:return 50
    return 60


def solar_opportunity(cfg, db, now, temperature=None):
    """Conservative stored-energy budget from the next useful daylight window.

    Use only forecasts already available at `now`. Subtract house/appliance load
    and overhead before crediting solar toward forecast-only cold preparation.
    """
    from zoneinfo import ZoneInfo
    from energy_strategy import learned_house_load_w, safe_forecast_factor, scheduled_load_remaining_kwh
    from charge_temperature import current_limit_a

    local = now.astimezone(ZoneInfo(cfg['site']['timezone']))
    for day in [local.date(), local.date() + dt.timedelta(days=1)]:
        rows = db.conn.execute('SELECT * FROM forecasts WHERE target_date=? ORDER BY id DESC LIMIT 80', (day.isoformat(),)).fetchall()
        for row in rows:
            try:
                fetched = dt.datetime.fromisoformat(row['fetched_at'])
                if not 0 <= (now - fetched).total_seconds() <= 10800:continue
                sunset = dt.datetime.fromisoformat(row['sunset'])
                end = sunset - dt.timedelta(hours=1)
                start = max(now, dt.datetime.fromisoformat(row['useful_pv_start']))
                if end <= start:break
                points = [(dt.datetime.fromisoformat(p['time']), float(p['predicted_w']))
                          for p in json.loads(row['points_json'] or '[]')]
                if not points or any(not math.isfinite(w) or w < 0 for t,w in points):continue
                points.sort()
                if points[0][0] > start or points[-1][0] < end:continue
                factor, source = safe_forecast_factor(cfg, db, local, day)
                factor = max(0, min(0.7, factor))
                house_w, load_source = learned_house_load_w(cfg, db, local)
                overhead = float(cfg.get('load_model', {}).get('system_overhead_w', 130))
                hours = (end - start).total_seconds() / 3600
                pv = 0.0
                net = 0.0
                charge = cfg.get('charge_temperature', {})
                charge_kw = current_limit_a(temperature, charge.get('capacity_ah', 0), charge.get('max_current_a', 0)) * 51.2 / 1000
                for i,(t,w) in enumerate(points[:-1]):
                    next_time = points[i+1][0]
                    if (next_time - t).total_seconds() > 1800:
                        if next_time > start and t < end:raise ValueError('forecast gap')
                    overlap = max(0, (min(end,next_time) - max(start,t)).total_seconds()) / 3600
                    pv += w * overlap / 1000
                    net += min(charge_kw, (w * factor - house_w - overhead) / 1000) * overlap
                scheduled = sum(max(0,scheduled_load_remaining_kwh(cfg,name,start.astimezone(local.tzinfo),default)[0]
                                      -scheduled_load_remaining_kwh(cfg,name,end.astimezone(local.tzinfo),default)[0])
                                for name,default in [('water_heater','10:00'),('cooker','18:00')])
                loads = (house_w + overhead) / 1000 * hours + scheduled
                efficiency = max(0.5,min(1,float(cfg.get('day_strategy', {}).get('charge_efficiency',0.91))))
                surplus = max(0,net - scheduled) * efficiency
                return {'valid': True, 'fetched_at': fetched.isoformat(), 'refill_at': end.isoformat(),
                        'window_start': start.isoformat(), 'safe_pv_kwh': round(pv*factor,3),
                        'load_kwh': round(loads,3), 'stored_surplus_kwh': round(surplus,3),
                        'factor':factor, 'factor_source':source, 'load_source':load_source}
            except (ValueError,TypeError,KeyError):
                continue
    return {'valid':False,'reason':'solar_forecast_missing_stale_or_incomplete'}


def build_plan(cfg, previous, history, now, weather=None, solar=None):
    house = cfg['house']; mode = house['mode']
    plan = {'mode': mode, 'updated_at': now.isoformat(), 'storage_soc_pct': house['storage_soc_pct']}
    latest = history[-1] if history else None
    fresh = latest is not None and 0 <= (now - latest[0]).total_seconds() <= 600
    temperature = latest[1] if fresh else None
    plan['battery_temperature_c'] = temperature
    solar_usable = False
    if solar and solar.get('valid'):
        try:
            age = (now - dt.datetime.fromisoformat(solar['fetched_at'])).total_seconds()
            lead = (dt.datetime.fromisoformat(solar['refill_at']) - now).total_seconds()
            energy = float(solar['stored_surplus_kwh'])
            solar_usable = 0 <= age <= 10800 and 0 < lead <= 129600 and math.isfinite(energy) and energy >= 0
        except (KeyError,TypeError,ValueError):
            pass
    # Low-current recovery remains below the independent total-charge guard.
    old_grid = previous.get('grid_charge_a', 10)
    plan['grid_charge_a'] = 5 if temperature is None or temperature < 7 or (old_grid == 5 and temperature < 9) else 10
    alerts = []
    if temperature is None:alerts.append('battery_temperature_stale')
    elif temperature < 5:
        charge = cfg.get('charge_temperature', {})
        if charge.get('cold_maintenance_enabled') and temperature >= charge['cold_maintenance_min_c']:
            alerts.append('cold_maintenance_only')
        else:
            alerts.append('charging_blocked_cold')
    elif temperature <= 7:alerts.append('battery_near_cold_cutoff')
    plan['alerts'] = alerts
    if mode == 'winter-away':
        plan.update(reserve_soc_pct=house['storage_soc_pct'], reason='winter_away_storage')
        return plan
    measured_floor = max(house['base_night_soc'], reserve_for_temperature(temperature)) if fresh else house['base_night_soc']
    reserve = measured_floor
    plan['measured_min_soc_pct'] = measured_floor
    forecast_ok = False
    urgent_cold = not fresh or temperature < 10
    if fresh and weather:
        age = (now - dt.datetime.fromisoformat(weather['fetched_at'])).total_seconds()
        end = now + dt.timedelta(hours=48)
        points = [(dt.datetime.fromisoformat(t), value) for t, value in weather.get('points', [])]
        relevant = [(t, v) for t, v in points if now <= t <= end]
        if 0 <= age <= 10800 and relevant and max(t for t, v in relevant) >= end - dt.timedelta(hours=2):
            forecast_ok = True
            outdoor_min = min(v for t, v in relevant)
            # Assume the full predicted outdoor drop can reach the indoor pack.
            # This deliberately errs toward early preparation, not precise indoor
            # forecasting; the 8C thermostat is never a guaranteed cell minimum.
            estimated = temperature + min(0, outdoor_min - weather['current_air_c'])
            slope = 0.0
            quiet = [(t, temp) for t, temp, power in history if power < 300 and t >= now - dt.timedelta(hours=6)]
            if len(quiet) >= 3 and (quiet[-1][0] - quiet[0][0]).total_seconds() >= 10800:
                slope = (quiet[-1][1] - quiet[0][1]) / ((quiet[-1][0] - quiet[0][0]).total_seconds() / 3600)
                estimated = min(estimated, temperature + min(0, slope) * 48)
            plan.update(estimated_min_battery_c=round(estimated, 1), forecast_min_air_c=outdoor_min,
                        forecast_source=weather['source'])
            reserve = max(reserve, reserve_for_temperature(estimated))
            if solar_usable:
                refill = dt.datetime.fromisoformat(solar['refill_at'])
                until_refill = [(t,v) for t,v in relevant if t <= refill]
                urgent_cold = (urgent_cold or any(temperature + min(0,v-weather['current_air_c']) <= 7 for t,v in until_refill)
                               or temperature + min(0,slope) * max(0,(refill-now).total_seconds()/3600) <= 7)
    old = previous.get('cold_target_soc_pct', previous.get('reserve_soc_pct', house['base_night_soc']))
    # Missing weather/telemetry must never release an existing cold reserve.
    if not fresh or not forecast_ok:
        reserve = max(reserve, old)
        plan['alerts'].append('forecast_unavailable_reserve_held')
    if reserve < old:
        since = previous.get('warming_since', now.isoformat())
        if (now - dt.datetime.fromisoformat(since)).total_seconds() < 86400:
            plan['warming_since'] = since
            reserve = old
    reserve = min(house['storage_soc_pct'], reserve)
    plan['cold_target_soc_pct'] = reserve
    plan['solar'] = solar or {'valid':False,'reason':'not_supplied'}
    plan['solar_deferral_kwh'] = 0
    if fresh and forecast_ok and solar_usable and not urgent_cold:
        batt = float(cfg['battery']['effective_kwh'])
        credit = max(0,float(solar['stored_surplus_kwh']))
        extra = max(0,reserve-measured_floor) / 100 * batt
        deferred = min(credit,extra)
        reserve = max(measured_floor, math.ceil(reserve - deferred/batt*100))
        plan['solar_deferral_kwh'] = round((plan['cold_target_soc_pct']-reserve)/100*batt,3)
    else:
        # Stale data must also preserve an already-applied native reserve.
        if not fresh or not forecast_ok:
            reserve = max(reserve,previous.get('reserve_soc_pct',reserve))
    plan.update(reserve_soc_pct=min(house['storage_soc_pct'], reserve),
                reason='occupied_solar_deferred_cold_preparation' if plan['solar_deferral_kwh'] > 0
                       else 'occupied_cold_preparation' if reserve > house['base_night_soc'] else 'occupied_normal')
    return plan
