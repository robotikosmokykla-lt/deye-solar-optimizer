#!/usr/bin/env python3
"""Find the inverter serial for this Deye account.

Every telemetry read and control order is addressed to a specific inverter serial,
but that serial is the one value nothing else can supply: it is not in the developer
portal, and the logger stick on the same plant has a different one. This asks the
API instead of asking you to read a label.

Runs on the four credentials alone, before a complete configuration exists, so it
deliberately does not use the validating config loader.

    sudo deyeopt-discover                       # read credentials from deye.env
    deyeopt-discover --env ./my.env
    deyeopt-discover --app-id X --app-secret Y --login me@example.com --password Z
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from config_loader import DEFAULT_ENV, read_env_file

# Device types the control path can actually drive. A COLLECTOR is the logger stick
# and a MICRO_INVERTER is a different product family; orders addressed to either fail.
CONTROLLABLE = {"INVERTER"}
KNOWN_OTHER = {"COLLECTOR": "logger / datalogger stick",
               "MICRO_INVERTER": "microinverter, not supported by this optimizer",
               "PV_MODULE": "panel-level device"}


def credentials(args) -> dict:
    """Credentials from flags, else the env file, else an interactive prompt."""
    values = {}
    if args.env and os.path.exists(args.env):
        try:
            values = read_env_file(args.env)
        except Exception as exc:
            print(f"warning: could not read {args.env}: {exc}", file=sys.stderr)
    def pick(flag, key, prompt, secret=False):
        if flag:
            return flag
        if values.get(key):
            return values[key]
        if not sys.stdin.isatty():
            sys.exit(f"missing {key}: pass --{key.lower().replace('deye_','').replace('_','-')} "
                     f"or provide it in {args.env}")
        return getpass.getpass(prompt) if secret else input(prompt)
    return {
        "app_id": pick(args.app_id, "DEYE_APP_ID", "Deye App ID: "),
        "app_secret": pick(args.app_secret, "DEYE_APP_SECRET", "Deye App Secret: ", True),
        "login": pick(args.login, "DEYE_LOGIN", "Deye account email: "),
        "password": pick(args.password, "DEYE_PASSWORD", "Deye account password: ", True),
        "base_url": args.base_url or values.get("DEYE_BASE_URL")
                    or "https://eu1-developer.deyecloud.com/v1.0",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover Deye station and inverter serials")
    ap.add_argument("--env", default=DEFAULT_ENV, help="read credentials from this .env")
    ap.add_argument("--app-id"); ap.add_argument("--app-secret")
    ap.add_argument("--login"); ap.add_argument("--password")
    ap.add_argument("--base-url", help="regional endpoint, e.g. eu1/us1")
    ap.add_argument("--all", action="store_true", help="list every device, not just inverters")
    args = ap.parse_args()

    c = credentials(args)
    from deye_api import DeyeAPIError, DeyeClient
    client = DeyeClient(c["base_url"], app_id=c["app_id"], app_secret=c["app_secret"],
                        login=c["login"], password=c["password"])
    try:
        client.get_token()
    except DeyeAPIError as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        print("Check the App ID/Secret, the account email and password, and that "
              f"{c['base_url']} is the region your developer account was issued for.",
              file=sys.stderr)
        return 2

    try:
        data = client.list_stations_with_devices()
    except DeyeAPIError as exc:
        print(f"Could not list stations: {exc}", file=sys.stderr)
        return 3

    stations = data.get("stationList") or []
    if not stations:
        print("Authenticated, but this account owns no plants.")
        return 4

    inverters = []
    for st in stations:
        print(f"\nStation {st.get('id')}  \"{st.get('name')}\"  "
              f"{st.get('installedCapacity')} kWp  {st.get('regionTimezone') or ''}")
        for item in st.get("deviceListItems") or []:
            sn, kind = item.get("deviceSn"), str(item.get("deviceType") or "")
            if kind in CONTROLLABLE:
                inverters.append((st.get("id"), sn))
                print(f"    {'INVERTER':<15}{sn}   <-- use this")
            elif args.all:
                print(f"    {kind:<15}{sn}   ({KNOWN_OTHER.get(kind, 'other device')})")
        if not args.all:
            others = sum(1 for i in (st.get("deviceListItems") or [])
                         if str(i.get("deviceType")) not in CONTROLLABLE)
            if others:
                print(f"    ({others} other device(s) hidden; --all to show)")

    print()
    if not inverters:
        print("No INVERTER-type device found. This optimizer drives hybrid inverters; "
              "microinverters and panel-level devices are not supported.")
        return 5
    if len(inverters) > 1:
        print("Several inverters found. Pick the one this installation should control:")
        for station_id, sn in inverters:
            print(f'    DEYE_STATION_ID={station_id}\n    DEYE_INVERTER_SN="{sn}"\n')
        return 0

    station_id, sn = inverters[0]
    print("Add to your .env:\n")
    print(f'    DEYE_INVERTER_SN="{sn}"')
    print(f"    DEYE_STATION_ID={station_id}    # optional, diagnostics only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
