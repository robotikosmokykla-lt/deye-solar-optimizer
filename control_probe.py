#!/usr/bin/env python3
"""Diagnostic Deye control-path observer for v3.1.0.

Uses the official /strategy/dynamicControl/read + readResult endpoints. It does not
change settings, but Deye creates an asynchronous READ orderId, so this tool can
occupy the device command queue. Do NOT run it concurrently with the optimizer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import time
from zoneinfo import ZoneInfo
from config_loader import DEFAULT_ENV, load_config, make_deye_client

from deye_api import flatten_device_latest, parse_deye_timestamp


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnostic Deye control-path observer")
    ap.add_argument("--env", "--config", dest="config", default=DEFAULT_ENV)
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--interval", type=float, default=60.0)
    args = ap.parse_args()

    cfg = load_config(args.config)
    tz = ZoneInfo(cfg["site"]["timezone"])
    client = make_deye_client(cfg)
    sn = str(cfg["deye"]["inverter_sn"])

    print("DIAGNOSTIC control probe. No setting is changed, but Deye READ orders are created.")
    print("Do NOT run this while deye-solar-optimizer.service is active.")
    print("time | device collection/SOC | control path | maxSellPower")
    deadline = time.monotonic() + max(1.0, args.minutes * 60.0)
    while time.monotonic() < deadline:
        now = dt.datetime.now(tz)
        device_text = "ERR"
        control_text = "ERR"
        max_sell = "n/a"
        try:
            dv = client.get_device_latest(sn)
            flat = flatten_device_latest(dv)
            ct = parse_deye_timestamp(flat.get("collectionTime"), tz)
            device_text = f"{ct.isoformat() if ct else 'n/a'} SOC={flat['metrics'].get('SOC')}"
        except Exception as exc:
            device_text = f"ERR {str(exc)[:90]}"

        try:
            rr = client.dynamic_control_read(sn)
            conn = int(rr.get("connectionStatus") or 0)
            order_id = int(rr.get("orderId") or 0)
            if conn == 1:
                control_text = f"ONLINE readOrder={order_id}"
                if order_id > 0:
                    for _ in range(4):
                        try:
                            result = client.dynamic_control_read_result(order_id)
                            if result.get("maxSellPower") is not None:
                                max_sell = str(result.get("maxSellPower"))
                                break
                        except Exception:
                            pass
                        time.sleep(2)
            else:
                control_text = f"OFFLINE collection={rr.get('collectionTime')}"
        except Exception as exc:
            control_text = f"ERROR {str(exc)[:100]}"

        print(f"{now.strftime('%H:%M:%S')} | {device_text} | {control_text} | {max_sell}", flush=True)
        time.sleep(max(15.0, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
