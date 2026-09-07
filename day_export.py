#!/usr/bin/env python3
"""Create a compact diagnostic bundle for today or a recent time window.

Examples:
  sudo deye-day-export                 # today since 00:00
  sudo deye-day-export 6               # last 6 hours
  sudo deye-day-export --hours 3
  sudo deye-day-export --since '2026-09-04 09:00'

The bundle is read-only with respect to the inverter. It snapshots/prunes SQLite,
filters JSONL events by timestamp, captures systemd journal/status and one current
/device/latest read. Secrets in deye.env are redacted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from zoneinfo import ZoneInfo

from config_loader import DEFAULT_ENV, load_config, make_deye_client, redact_env_lines

VERSION = "3.1.0"


def parse_since(text: str, tz: ZoneInfo) -> dt.datetime:
    text = text.strip()
    try:
        value = dt.datetime.fromisoformat(text)
    except ValueError:
        value = dt.datetime.strptime(text, "%Y-%m-%d %H:%M")
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(tz)


def run_to_file(cmd: list[str], path: Path) -> None:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        path.write_text(p.stdout, encoding="utf-8")
    except Exception as exc:
        path.write_text(f"ERROR running {cmd!r}: {exc}\n", encoding="utf-8")


def filter_jsonl(src: Path, dst: Path, since_utc: dt.datetime) -> None:
    if not src.exists():
        dst.write_text("", encoding="utf-8")
        return
    kept = 0
    with src.open("r", encoding="utf-8", errors="replace") as inf, dst.open("w", encoding="utf-8") as outf:
        for line in inf:
            try:
                rec = json.loads(line)
                ts = dt.datetime.fromisoformat(str(rec.get("ts")))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                if ts.astimezone(dt.timezone.utc) >= since_utc:
                    outf.write(line)
                    kept += 1
            except Exception:
                continue
    if kept == 0:
        dst.write_text("", encoding="utf-8")


def prune_db(src_path: Path, dst_path: Path, since_iso: str) -> None:
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    src.backup(dst)
    src.close()
    # Keep KV state in full; prune time-series tables to the requested window.
    table_cols = {
        "telemetry": "observed_at",
        "forecasts": "fetched_at",
        "decisions": "ts",
        "writes": "ts",
        "api_events": "ts",
    }
    for table, col in table_cols.items():
        try:
            dst.execute(f"DELETE FROM {table} WHERE {col} < ?", (since_iso,))
        except sqlite3.Error:
            pass
    dst.commit()
    try:
        dst.execute("VACUUM")
    except sqlite3.Error:
        pass
    dst.close()


def db_summary(db_path: Path, since_iso: str) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out: list[str] = []
    def show(title: str, sql: str, args=()):
        out.extend(["", "=" * 90, title, "=" * 90])
        try:
            rows = conn.execute(sql, args).fetchall()
            out.append(f"rows: {len(rows)}")
            out.extend(str(dict(r)) for r in rows)
        except Exception as exc:
            out.append(f"ERROR: {exc!r}")
    show("WRITES", "SELECT * FROM writes WHERE ts>=? ORDER BY ts", (since_iso,))
    show("TELEMETRY SUMMARY", "SELECT id,observed_at,logger_at,soc,control_soc,soc_confidence,generation_power,consumption_power,grid_power,battery_power,device_state,daily_production_kwh,daily_consumption_kwh,total_buy_kwh,total_sell_kwh,total_charge_kwh,total_discharge_kwh FROM telemetry WHERE observed_at>=? ORDER BY observed_at", (since_iso,))
    show("FORECASTS", "SELECT * FROM forecasts WHERE fetched_at>=? ORDER BY fetched_at", (since_iso,))
    show("DECISIONS", "SELECT * FROM decisions WHERE ts>=? ORDER BY ts", (since_iso,))
    show("API EVENTS", "SELECT * FROM api_events WHERE ts>=? ORDER BY ts", (since_iso,))
    show("KV STATE", "SELECT * FROM kv ORDER BY key")
    conn.close()
    return "\n".join(out).lstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Export compact Deye diagnostics")
    ap.add_argument("hours_pos", nargs="?", type=float, help="optional shorthand: last N hours")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--hours", type=float, help="last N hours")
    group.add_argument("--since", help="local timestamp, e.g. '2026-09-04 09:00'")
    group.add_argument("--today", action="store_true", help="from local midnight (default)")
    ap.add_argument("--env", "--config", dest="env_path", default=DEFAULT_ENV)
    ap.add_argument("--output-dir", help="override LOGGING_BUNDLE_DIR")
    ap.add_argument("--full-db", action="store_true", help="include full historical state.db instead of pruned window copy")
    args = ap.parse_args()

    cfg = load_config(args.env_path)
    tz = ZoneInfo(cfg["site"]["timezone"])
    now = dt.datetime.now(tz)
    hours = args.hours if args.hours is not None else args.hours_pos
    if hours is not None:
        if hours <= 0 or hours > 24 * 31:
            raise SystemExit("--hours must be >0 and <=744")
        since = now - dt.timedelta(hours=float(hours))
        label = f"{float(hours):g}h"
    elif args.since:
        since = parse_since(args.since, tz)
        label = "since"
    else:
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "today"
    if since > now:
        raise SystemExit("start time is in the future")

    out_dir = Path(args.output_dir or cfg["logging"].get("bundle_dir") or "/var/lib/deye-solar-optimizer/exports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    dirname = f"deye-check-{stamp}-{label}"
    work_parent = Path(tempfile.mkdtemp(prefix="deye-export-"))
    work = work_parent / dirname
    work.mkdir()
    archive = out_dir / f"{dirname}.tar.gz"

    try:
        (work / "window.txt").write_text(
            f"version={VERSION}\nstart={since.isoformat()}\nend={now.isoformat()}\nwindow_hours={(now-since).total_seconds()/3600:.3f}\n",
            encoding="utf-8",
        )
        (work / "server-time.txt").write_text(now.isoformat() + "\n", encoding="utf-8")
        run_to_file(["timedatectl"], work / "timedatectl.txt")
        run_to_file(["systemctl", "--no-pager", "--full", "status", "deye-solar-optimizer"], work / "systemd-status.txt")
        run_to_file(["journalctl", "-u", "deye-solar-optimizer", "--since", since.strftime("%Y-%m-%d %H:%M:%S"), "--no-pager", "-o", "short-iso-precise"], work / "controller-journal.log")
        run_to_file(["/usr/local/bin/deyeopt-status", "--env", args.env_path], work / "deyeopt-status.txt")
        run_to_file(["/usr/local/bin/deyeopt-day-plan", "--env", args.env_path], work / "day-plan.txt")
        run_to_file(["/usr/local/bin/deyeopt-strategy", "status", "--env", args.env_path], work / "strategy-status.txt")
        (work / "config-redacted.env").write_text(redact_env_lines(args.env_path), encoding="utf-8")
        try:
            (work / "version.txt").write_text(Path("/opt/deye-solar-optimizer/VERSION").read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            (work / "version.txt").write_text(VERSION + "\n", encoding="utf-8")

        src_db = Path(cfg["logging"]["state_db"])
        dst_db = work / "state.db"
        if args.full_db:
            src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
            dst = sqlite3.connect(dst_db)
            src.backup(dst); dst.close(); src.close()
        else:
            prune_db(src_db, dst_db, since.isoformat())
        (work / "db-summary.txt").write_text(db_summary(dst_db, since.isoformat()), encoding="utf-8")

        filter_jsonl(Path(cfg["logging"]["jsonl_log"]), work / "events.jsonl", since.astimezone(dt.timezone.utc))

        try:
            client = make_deye_client(cfg)
            r = client.get_device_latest(str(cfg["deye"]["inverter_sn"]))
            (work / "device-latest-now.json").write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception as exc:
            (work / "device-latest-now-error.txt").write_text(repr(exc) + "\n", encoding="utf-8")

        with tarfile.open(archive, "w:gz") as tf:
            tf.add(work, arcname=dirname)
    finally:
        shutil.rmtree(work_parent, ignore_errors=True)

    try:
        user = os.environ.get("SUDO_USER")
        if user:
            import pwd
            pw = pwd.getpwnam(user)
            os.chown(archive, pw.pw_uid, pw.pw_gid)
    except Exception:
        pass

    print(f"Deye diagnostic bundle: {archive}")
    print(f"Window: {since.isoformat()} -> {now.isoformat()} ({(now-since).total_seconds()/3600:.2f} h)")
    print(f"Size: {archive.stat().st_size/1024:.1f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
