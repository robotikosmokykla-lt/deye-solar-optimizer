#!/usr/bin/env python3
"""Explicit occupied / winter-away switch. Never infer absence from temperatures."""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from config_loader import DEFAULT_ENV, load_config


def update_mode(path, mode):
    cfg = load_config(str(path))
    if not cfg['charge_temperature']['enabled']:
        raise ValueError('Enable BATTERY_TEMP_CHARGE_ENABLED before switching house modes')
    updates = {'HOUSE_RESERVE_ENABLED': 'true', 'HOUSE_MODE': mode}
    lines = path.read_text().splitlines(); seen = set()
    for i, line in enumerate(lines):
        key = line.strip().removeprefix('export ').split('=', 1)[0].strip()
        if key in updates:
            lines[i] = key + '=' + updates[key]; seen.add(key)
    lines.extend(key + '=' + value for key, value in updates.items() if key not in seen)
    backup = path.with_name(path.name + '.mode-backup-' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(path, backup)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'w') as output:
            output.write('\n'.join(lines) + '\n'); output.flush(); os.fsync(output.fileno())
        stat = path.stat(); os.chmod(name, stat.st_mode & 0o777)
        os.chown(name, stat.st_uid, stat.st_gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name):os.unlink(name)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['status', 'occupied', 'winter-away'], nargs='?', default='status')
    parser.add_argument('--env', default=DEFAULT_ENV)
    parser.add_argument('--preview', action='store_true', help='No file changes, API calls, or service restarts')
    args = parser.parse_args(); cfg = load_config(args.env)
    mode = cfg['house']['mode'] if args.mode == 'status' else args.mode
    if args.mode == 'status' or args.preview:
        if args.preview:
            from house_policy import apply_plan
            cfg['house'].update(enabled=True, mode=mode)
            apply_plan(cfg, {})
        print(f"House mode: {mode}; enabled={cfg['house']['enabled']}; winter-away storage={cfg['house']['storage_soc_pct']}%")
        print(f"Resolved reserve: day {cfg['battery']['soc_floor_pct']}%, night {cfg['night_strategy']['morning_soc_pct']}%")
        if args.preview:print('Preview only: requested mode has not been applied.')
        return 0
    lock_path = Path(cfg['logging']['state_db']).parent / 'mode.lock'
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print('Config backup:', update_mode(Path(args.env), mode), flush=True)
        # Keep config edits and the native application serialized across CLI and
        # dashboard callers. The native manager owns the shared inverter queue.
        return subprocess.run([sys.executable, str(Path(__file__).with_name('season_manager.py')),
                               '--env', args.env, '--apply', '--force'], check=False).returncode


if __name__ == '__main__':
    try:raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f'Mode switch failed: {exc}');raise SystemExit(1)
