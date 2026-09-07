#!/usr/bin/env python3
"""Quick local strategy-tag switcher for Deye Solar Optimizer v3.1.0.

Edits only STRATEGY_ACTIVE in /etc/deye-solar-optimizer/deye.env and optionally
restarts the service. It never calls the Deye API itself.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from config_loader import DEFAULT_ENV, load_config
from strategy_presets import PRESETS, normalize_strategy_tag

VERSION = "3.1.0"


def read_active(path: Path) -> str:
    cfg = load_config(str(path))
    return normalize_strategy_tag(cfg.get("strategy", {}).get("active", "conservative"))


def update_active(path: Path, tag: str) -> Path:
    lines = path.read_text(encoding="utf-8").splitlines()
    found = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        prefix = "export " if stripped.startswith("export ") else ""
        core = stripped[7:].lstrip() if prefix else stripped
        if core.startswith("STRATEGY_ACTIVE="):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f'{indent}{prefix}STRATEGY_ACTIVE="{tag}"')
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.extend(["# Active operating strategy", f'STRATEGY_ACTIVE="{tag}"'])

    backup = path.with_name(path.name + f".strategy-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    payload = "\n".join(out).rstrip() + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, path.stat().st_mode & 0o777)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    check = read_active(path)
    if check != tag:
        raise RuntimeError(f"strategy verification failed: wrote {tag}, parsed {check}")
    return backup


def main() -> int:
    ap = argparse.ArgumentParser(description="Switch Deye optimizer strategy tag")
    ap.add_argument("tag", nargs="*", help="conservative | risky | max-export | save | economic | status | list")
    ap.add_argument("--env", "--config", dest="config", default=DEFAULT_ENV)
    ap.add_argument("--no-restart", action="store_true", help="Change .env but do not restart the service")
    args = ap.parse_args()
    path = Path(args.config)
    if not path.exists():
        print(f"ERROR: env file not found: {path}")
        return 2

    text = " ".join(args.tag).strip() or "status"
    if text.lower() in {"status", "show"}:
        active = read_active(path)
        print(f"Deye strategy manager v{VERSION}")
        print(f"ACTIVE: {active}")
        print(PRESETS[active].description)
        return 0
    if text.lower() in {"list", "ls"}:
        active = read_active(path)
        print(f"Deye strategy manager v{VERSION}")
        for tag, policy in PRESETS.items():
            mark = "*" if tag == active else " "
            print(f"{mark} {tag:13s} {policy.description}")
        return 0

    try:
        tag = normalize_strategy_tag(text)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    before = read_active(path)
    if before == tag:
        print(f"ACTIVE already: {tag}")
        print(PRESETS[tag].description)
        return 0

    backup = update_active(path, tag)
    print(f"Strategy: {before} -> {tag}")
    print(PRESETS[tag].description)
    print(f"Env backup: {backup}")
    print("No Deye API command was sent by this utility.")

    if not args.no_restart:
        try:
            subprocess.run(["systemctl", "restart", "deye-solar-optimizer.service"], check=True)
            print("Service restarted. Controller will apply the tag under its guarded write rules.")
        except Exception as exc:
            print(f"WARNING: .env changed but service restart failed: {exc}")
            return 1
    else:
        print("Service was NOT restarted (--no-restart).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
