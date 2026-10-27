#!/usr/bin/env python3
"""Scan publication candidates and optional history, printing locations, not values.

Examples:
  python3 tools/privacy_check.py
  sudo python3 tools/privacy_check.py --compare-env /etc/deye-solar-optimizer/deye.env
  sudo python3 tools/privacy_check.py --compare-env /etc/deye-solar-optimizer/deye.env --history main

This is a targeted privacy check, not a guarantee that arbitrary secrets or
identifying images are absent. Screenshots and commit metadata need review too.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

PATTERNS = {
    'private_key': rb'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----',
    'github_token': rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b',
    'aws_access_key': rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'user_home_path': rb'/(?:home|Users)/[A-Za-z0-9_.-]+/',
}
PRIVATE_KEYS = {
    'DEYE_APP_ID', 'DEYE_APP_SECRET', 'DEYE_LOGIN', 'DEYE_PASSWORD',
    'DEYE_INVERTER_SN', 'DEYE_STATION_ID', 'SITE_LATITUDE', 'SITE_LONGITUDE',
    'HOUSE_WEATHER_STATION', 'ANALYTICS_ALLOWED_HOSTS', 'LOGGING_BUNDLE_DIR',
}


def scan_bytes(data, known_values):
    findings = []
    for number, line in enumerate(data.splitlines(), 1):
        for category, pattern in PATTERNS.items():
            if re.search(pattern, line):findings.append((number, category))
        for key, value in known_values.items():
            if value in line:findings.append((number, 'deployment_value:' + key))
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--compare-env', type=Path, help='Private deployment .env, read only in memory')
    ap.add_argument('--history', help='Also inspect every blob reachable from this local Git ref')
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]

    def git(*arguments):
        return subprocess.check_output(['git', *arguments], cwd=root)

    known = {}
    if args.compare_env:
        sys.path.insert(0, str(root))
        from config_loader import read_env_file
        for key, value in read_env_file(args.compare_env).items():
            if key == 'LOGGING_BUNDLE_DIR' and value.startswith(('/var/lib/deye-solar-optimizer/', '/tmp/deye-demo/')):
                continue
            if key in PRIVATE_KEYS and len(value) >= 5:
                known[key] = value.encode()
    results = []
    candidates = sorted(set(git('ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0')) - {b''})
    for raw in candidates:
        name = raw.decode()
        path = root / name
        if path.is_symlink():
            data = path.readlink().as_posix().encode()
        elif path.is_file():
            data = path.read_bytes()
        else:
            continue
        for line, category in scan_bytes(data, known):
            results.append(dict(scope='working_tree', file=name, line=line, category=category))
    history_blobs = 0
    if args.history:
        git('rev-parse', '--verify', args.history + '^{commit}')
        for row in git('rev-list', '--objects', args.history).decode().splitlines():
            oid, _, name = row.partition(' ')
            if git('cat-file', '-t', oid).strip() != b'blob':continue
            history_blobs += 1
            for line, category in scan_bytes(git('cat-file', 'blob', oid), known):
                results.append(dict(scope='history', object=oid, file=name, line=line, category=category))
    print(json.dumps({'candidate_files': len(candidates), 'history_blobs': history_blobs, 'findings': results}, indent=2))
    return 1 if results else 0


if __name__ == '__main__':
    raise SystemExit(main())
