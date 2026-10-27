#!/usr/bin/env python3
"""Narrow local mode-control broker. Only the two fixed mode commands are allowed.

The analytics process remains unprivileged/read-only. This root-owned service
accepts requests on a group-restricted Unix socket and invokes the same guarded
CLI as an operator. No HTTP listener, shell commands or user-selected paths.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import pwd
import socket
import socketserver
import sqlite3
import signal
import struct
import subprocess
import sys
import threading
import uuid

from config_loader import DEFAULT_ENV, load_config

SOCKET = '/run/deye-solar-mode/control.sock'
MODES = {'occupied', 'winter-away'}


def mode_status(cfg):
    directory = Path(cfg['logging']['state_db']).parent
    try:
        state = json.loads((directory / 'season-state.json').read_text())
    except FileNotFoundError:
        state = {}
    applied = json.loads(state.get('applied', '{}'))
    verified = state.get('verified', {})
    slots = verified.get('timeUseSettingItems', [])
    house = cfg.get('house', {})
    from state_db import StateDB
    try:
        db = StateDB(cfg['logging']['state_db'], readonly=True)
        try:
            temperature = db.get('charge_temperature_state', {}) or {}
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        temperature = {}
    alerts = list(dict.fromkeys(house.get('plan', {}).get('alerts', []) + temperature.get('alerts', [])))
    return {'requested_mode': house.get('mode', 'occupied'),
            'confirmed_mode': applied.get('house_mode'),
            'confirmed_at': state.get('confirmed_at'),
            'day_reserve_pct': next((x['soc'] for x in slots if x['time'] == '07:00'), None),
            'night_reserve_pct': next((x['soc'] for x in slots if x['time'] == '21:00'), None),
            'grid_charge_a': verified.get('gridChargeAmpere'),
            'storage_soc_pct': house.get('storage_soc_pct', 60),
            'alerts': alerts,
            'critical_action': temperature.get('critical_action')}


def broker_request(payload, path=SOCKET):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(path)
        client.sendall(json.dumps(payload).encode() + b'\n')
        with client.makefile('rb') as stream:
            line = stream.readline(16385)
        if len(line) > 16384:
            raise ValueError('Oversized mode-control response')
        return json.loads(line)


class ModeController:
    def __init__(self, env_path=DEFAULT_ENV):
        self.env_path = env_path
        self.lock = threading.Lock()
        self.job = None
        self.worker = None

    def status(self):
        status = mode_status(load_config(self.env_path))
        with self.lock:
            status['job'] = dict(self.job) if self.job else None
        return status

    def start(self, mode):
        if mode not in MODES:
            return {'error': 'Invalid house mode', 'status': 400}
        cfg = load_config(self.env_path)
        if not cfg.get('analytics', {}).get('mode_control_enabled', False):
            return {'error': 'Dashboard mode control is disabled', 'status': 403}
        if cfg['control']['dry_run'] or not cfg['charge_temperature']['enabled']:
            return {'error': 'Mode switching requires live temperature charging protection', 'status': 403}
        with self.lock:
            if self.job and self.job['state'] == 'applying':
                if self.job['mode'] == mode:
                    return {'job': dict(self.job), 'status': 202}
                return {'error': 'A mode change is already applying', 'status': 409}
            status = mode_status(cfg)
            if status['confirmed_mode'] == mode and status['requested_mode'] == mode:
                return {'job': {'mode': mode, 'state': 'confirmed', 'message': 'Mode is already confirmed.'}, 'status': 200}
            self.job = {'id': uuid.uuid4().hex, 'mode': mode, 'state': 'applying',
                        'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                        'message': 'Applying mode; waiting for inverter confirmation.'}
            job = dict(self.job)
        self.worker = threading.Thread(target=self.run, args=(job['id'], mode), daemon=True)
        self.worker.start()
        return {'job': job, 'status': 202}

    def run(self, job_id, mode):
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('mode_manager.py')),
                                     mode, '--env', self.env_path],
                                    capture_output=True, text=True, timeout=480, check=False)
            print(result.stdout, flush=True)
            if result.stderr:print(result.stderr, file=sys.stderr, flush=True)
            confirmed = mode_status(load_config(self.env_path))['confirmed_mode']
            success = result.returncode == 0 and confirmed == mode
            message = 'Mode confirmed by inverter.' if success else 'Mode change was not confirmed. Check the current mode and retry.'
        except Exception as exc:
            print(f'Mode change error: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
            success, message = False, 'Mode change was not confirmed. Check the current mode before retrying.'
        with self.lock:
            if self.job and self.job['id'] == job_id:
                self.job.update(state='confirmed' if success else 'failed', message=message,
                                finished_at=dt.datetime.now(dt.timezone.utc).isoformat())


class SocketHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            # Only the analytics account and root may ask this service to act.
            peer = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack('3i', peer)
            if uid not in {0, pwd.getpwnam('deyeopt').pw_uid}:
                raise PermissionError('Unauthorized mode-control client')
            line = self.rfile.readline(513)
            if len(line) > 512:raise ValueError('Oversized request')
            request = json.loads(line)
            if request == {'op': 'status'}:
                result = self.server.controller.status()
            elif set(request) == {'op', 'mode'} and request['op'] == 'switch' and isinstance(request['mode'], str):
                result = self.server.controller.start(request['mode'])
            else:
                result = {'error': 'Invalid mode-control request', 'status': 400}
        except Exception:
            result = {'error': 'Mode-control request rejected', 'status': 400}
        self.wfile.write(json.dumps(result).encode() + b'\n')


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', default=DEFAULT_ENV)
    parser.add_argument('--socket', default=SOCKET)
    args = parser.parse_args()
    path = Path(args.socket)
    if path.exists():path.unlink()
    with UnixServer(str(path), SocketHandler) as server:
        os.chmod(path, 0o660)
        server.controller = ModeController(args.env)
        def shutdown(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        server.serve_forever()
        if server.controller.worker:
            server.controller.worker.join(timeout=480)


if __name__ == '__main__':main()
