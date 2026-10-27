"""Supplemental cloud charging limits using the Victron Lithium NG chart.

This does not replace local BMS temperature protection. Limit total charging,
not just grid charging. Lower limits apply immediately; increases need fresh
telemetry, hysteresis and a stable recovery interval. All orders share the
optimizer's command queue, and accepted IDs survive process restarts.
"""
from __future__ import annotations

import datetime as dt
import math

from deye_api import is_busy_error, is_offline_error

KEY = "charge_temperature_state"


def blocks_command_queue(state, now):
    if state.get('pending'):
        return True
    until = state.get('retry_after')
    if not until or now >= dt.datetime.fromisoformat(until):
        return False
    uncertain = state.get('uncertain_submission')
    if uncertain is None:
        error = state.get('last_error', '')
        uncertain = not (is_offline_error(RuntimeError(error)) or is_busy_error(RuntimeError(error))
                         or error.startswith(('write rejected:', 'readback mismatch:')))
    return uncertain


def current_limit_a(temperature, capacity_ah, ceiling_a):
    if temperature is None or not math.isfinite(temperature) or temperature < 5 or temperature >= 50:
        return 0
    rate = 0.2 if temperature < 10 else 0.3 if temperature < 15 or temperature >= 35 else 0.5
    return max(0, min(int(ceiling_a), math.floor(capacity_ah * rate)))


def recovery_margin_ok(temperature, target, capacity_ah, ceiling_a, margin):
    # Apply the recovery margin to every boundary that permits this higher band.
    if temperature is None:
        return False
    rate = target / capacity_ah
    lower = 15 if rate > 0.3 + 1e-9 else 10 if rate > 0.2 + 1e-9 else 5
    upper = 35 if rate > 0.3 + 1e-9 else 50
    return lower + margin <= temperature <= upper - margin


def cold_maintenance_limit(temperature, soc, options, house, state):
    """Optional user-selected departure from Victron, never a BMS override.

    Only fresh, above-freezing measurements may authorize maintenance. Keep the
    slow mode through the normal cold-recovery margin and stop at the storage
    ceiling in occupied as well as away mode.
    """
    if not options.get('cold_maintenance_enabled', False):
        state.pop('cold_maintenance_active', None)
        state.pop('cold_maintenance_hold', None)
        return None, None
    active = state.get('cold_maintenance_active', False)
    if temperature is not None:
        if temperature < 5:active = True
        elif temperature >= 5 + options['hysteresis_c']:active = False
    state['cold_maintenance_active'] = active
    if not active:
        state.pop('cold_maintenance_hold', None)
        return None, None
    if temperature is None or temperature < options['cold_maintenance_min_c']:
        return 0, 'cold_maintenance_temperature_blocked'
    if soc is None or not math.isfinite(soc) or not 0 <= soc <= 100:
        return 0, 'cold_maintenance_soc_unknown'
    ceiling_soc = min(60, house.get('storage_soc_pct', 60))
    hold = state.get('cold_maintenance_hold', False)
    if soc >= ceiling_soc:hold = True
    elif soc <= ceiling_soc - 5:hold = False
    state['cold_maintenance_hold'] = hold
    if hold:
        return 0, 'cold_maintenance_storage_soc_hold'
    limit = min(options['cold_maintenance_current_a'], options['max_current_a'],
                math.floor(options['capacity_ah'] * 0.02))
    return limit, 'cold_maintenance_replenishment'


class ChargeTemperatureGuard:
    def __init__(self, cfg, db, client, log):
        self.cfg, self.db, self.client, self.log = cfg, db, client, log
        self.options = cfg.get("charge_temperature", {})
        self.state = db.get(KEY, {}) or {}

    def save(self, now):
        self.db.set(KEY, self.state, now)

    def poll(self, now):
        pending = self.state.get("pending")
        if not pending:
            return False
        poll_at = self.state.get("poll_at")
        if poll_at and now < dt.datetime.fromisoformat(poll_at):
            return True
        self.state["poll_at"] = (now + dt.timedelta(seconds=15)).isoformat()
        try:
            if pending["kind"] == "write":
                result = self.client.check_order_status(pending["id"])
                status = str(result.get("status"))
                if status == "666":
                    # Do not claim a limit is active until independent readback.
                    pending["kind"] = "verify_needed"
                elif status == "500":
                    self.failed(now, f"write rejected: {result.get('error')}")
                self.save(now)
                return True
            if pending["kind"] == "verify_needed":
                result = self.client.dynamic_control_read(self.cfg["deye"]["inverter_sn"])
                order = int(result.get("orderId") or 0)
                if order > 0:
                    pending.update(kind="read", id=order)
                self.save(now)
                return True
            result = self.client.dynamic_control_read_result(pending["id"])
            value = result.get("maxChargeCurrent")
            if value is None:
                self.save(now)
                return True
            if int(value) != pending["target_a"]:
                self.failed(now, f"readback mismatch: requested {pending['target_a']}A, read {value}A")
                return True
            previous = self.state.get("confirmed_a")
            if previous is not None and int(value) > previous:
                self.state["raises_today"] = self.state.get("raises_today", 0) + 1
            self.state.update(confirmed_a=int(value), verified_at=now.isoformat(), last_error=None, uncertain_submission=False)
            self.state.pop("pending", None)
            self.state.pop("retry_after", None)
            self.db.add_api_event(now, "temperature_limit_verified", True, f"MAX_CHARGE_CURRENT={value}A")
            self.log.emit("INFO", "temperature_limit_verified", current_a=int(value))
            self.save(now)
        except Exception as exc:
            self.state["last_error"] = str(exc)[:500]
            self.save(now)
            self.log.emit("WARN", "temperature_order_poll_failed", error=str(exc))
        return True

    def failed(self, now, message):
        self.state.update(confirmed_a=None, last_error=message,
                          retry_after=(now + dt.timedelta(minutes=45)).isoformat(), uncertain_submission=False)
        self.state.pop("pending", None)
        self.db.add_api_event(now, "temperature_limit_failed", False, message)
        self.log.emit("ERROR", "temperature_limit_failed", error=message)
        self.save(now)

    def observe_soc(self, now, snapshot):
        soc = None
        temperature = None
        if snapshot is not None and snapshot.collection_at is not None:
            age = (now - snapshot.collection_at).total_seconds() / 60
            if -1 <= age <= self.options.get('max_age_minutes', 10):
                try:
                    soc = float(snapshot.metrics.get('SOC'))
                    if not math.isfinite(soc) or not 0 <= soc <= 100:soc = None
                except (ValueError, TypeError):
                    pass
        was_critical = self.state.get('critical_low_soc', False)
        critical = was_critical
        if soc is not None and math.isfinite(soc) and 0 <= soc <= 100:
            if soc <= 10:critical = True
            elif soc >= 15:critical = False
        self.state['critical_low_soc'] = critical
        self.state['alerts'] = ['critical_low_soc'] if critical else []
        if critical:
            self.state['critical_action'] = 'Restore reserve only within permitted cell temperatures; warm or isolate if cold. Software charge limits do not stop standby draw.'
            if not was_critical:
                self.log.emit('ERROR', 'critical_low_soc', soc=soc, temperature_c=temperature,
                              action=self.state['critical_action'])
        else:
            self.state.pop('critical_action', None)
        self.save(now)
        return soc

    def tick(self, now, snapshot, *, export_pending=False):
        """Return True while normal export orders must wait for this guard."""
        if self.state.get("day") != now.date().isoformat():
            self.state.update(day=now.date().isoformat(), raises_today=0)
        soc = self.observe_soc(now, snapshot)
        if self.poll(now):
            return True
        if not self.options.get("enabled", False):
            return False
        temperature = None
        reason = "temperature_missing_or_stale"
        if snapshot is not None and snapshot.collection_at is not None:
            age = (now - snapshot.collection_at).total_seconds() / 60
            if -1 <= age <= self.options["max_age_minutes"]:
                try:
                    value = float(snapshot.metrics.get("Temperature- Battery"))
                    if math.isfinite(value) and -50 <= value <= 100:
                        temperature = value
                        reason = "victron_temperature_band"
                except (ValueError, TypeError):
                    pass
        capacity = self.options["capacity_ah"]
        ceiling = self.options["max_current_a"]
        target = current_limit_a(temperature, capacity, ceiling)
        house = self.cfg.get('house', {})
        maintenance, maintenance_reason = cold_maintenance_limit(temperature, soc, self.options, house, self.state)
        if maintenance is not None:
            target, reason = maintenance, maintenance_reason
        if house.get('enabled') and house['mode'] == 'winter-away':
            hold = self.state.get('storage_charge_hold', False)
            if soc is not None and math.isfinite(soc):
                if soc >= house['storage_soc_pct']:hold = True
                elif soc <= house['storage_soc_pct'] - 5:hold = False
            else:
                hold = True
            self.state['storage_charge_hold'] = hold
            if hold:
                target = 0
                reason = 'winter_away_storage_charge_hold'
        else:
            self.state.pop('storage_charge_hold', None)
        current = self.state.get("confirmed_a")
        self.state.update(temperature_c=temperature, recommended_a=target, reason=reason)
        # Unknown setting: establish a restrictive limit; never assume a cached
        # setting survived a failed command or an earlier untracked manual edit.
        if current is None:
            target = min(target, math.floor(capacity * 0.2))
        elif target > current:
            stable = self.state.get("recovery", {})
            margin_ok = (temperature is not None and temperature >= self.options['cold_maintenance_min_c'] + 0.5
                         if maintenance is not None and target > 0
                         else recovery_margin_ok(temperature, target, capacity, ceiling, self.options["hysteresis_c"]))
            if not margin_ok:
                self.state.pop("recovery", None)
                self.state["reason"] = "temperature_recovery_hysteresis"
                self.save(now)
                return False
            if stable.get("target_a") != target:
                stable = {"target_a": target, "since": now.isoformat()}
                self.state["recovery"] = stable
            elapsed = (now - dt.datetime.fromisoformat(stable["since"])).total_seconds() / 60
            if (elapsed < self.options["recovery_minutes"]
                    or (self.state.get("raises_today", 0) >= 4 and maintenance is None)):
                self.state["reason"] = "temperature_recovery_wait"
                self.save(now)
                return False
        else:
            self.state.pop("recovery", None)
        self.state["target_a"] = target
        self.save(now)
        if current == target:
            return False
        if self.cfg["control"].get("dry_run", True):
            self.state["reason"] = "temperature_dry_run"
            self.save(now)
            return False
        if export_pending:
            return True
        for guard in [self.state.get("retry_after"), self.db.get("uncertain_write_guard_until")]:
            if guard and now < dt.datetime.fromisoformat(guard):
                return blocks_command_queue(self.state, now)
        # Persist a guard before the request. A lost acknowledgement may conceal
        # an accepted command. Never submit another command while this is uncertain.
        self.state["retry_after"] = (now + dt.timedelta(minutes=120)).isoformat()
        self.state['uncertain_submission'] = True
        self.state.pop("recovery", None)
        self.save(now)
        try:
            result = self.client.set_battery_parameter(self.cfg["deye"]["inverter_sn"], "MAX_CHARGE_CURRENT", target)
            order = int(result.get("orderId") or 0)
            if order <= 0:
                self.state["retry_after"] = (now + dt.timedelta(seconds=60)).isoformat()
                self.state['uncertain_submission'] = False
            else:
                self.state['uncertain_submission'] = False
                self.state["pending"] = {"kind": "write", "id": order, "target_a": target, "submitted_at": now.isoformat()}
                self.state["poll_at"] = now.isoformat()
                self.db.add_api_event(now, "temperature_limit_submitted", True, f"orderId={order} target={target}A")
                self.log.emit("INFO", "temperature_limit_submitted", order_id=order, current_a=target, temperature_c=temperature)
        except Exception as exc:
            if is_offline_error(exc) or is_busy_error(exc):
                self.state["retry_after"] = (now + dt.timedelta(seconds=60)).isoformat()
                self.state['uncertain_submission'] = False
            else:
                self.state["confirmed_a"] = None
            self.state["last_error"] = str(exc)[:500]
            self.log.emit("ERROR", "temperature_limit_submit_failed", error=str(exc), target_a=target)
        self.save(now)
        return blocks_command_queue(self.state, now)
