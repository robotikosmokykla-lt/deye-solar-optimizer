#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/deye-solar-optimizer
ETC=/etc/deye-solar-optimizer
ENV_FILE="$ETC/deye.env"
LEGACY_CFG="$ETC/config.toml"
LEGACY_CRED="$ETC/credentials"
STAMP="$(date +%Y%m%d-%H%M%S)"
WATER_HEATER_TIME_OVERRIDE=""

usage() {
  cat <<USAGE
Usage: sudo ./upgrade.sh [--water-heater-time HH:MM]

Migrates v2 configuration to v3 deye.env when needed.
Optional --water-heater-time overrides the migrated/current heater planning time.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --water-heater-time) WATER_HEATER_TIME_OVERRIDE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -n "$WATER_HEATER_TIME_OVERRIDE" && ! "$WATER_HEATER_TIME_OVERRIDE" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
  echo "Invalid --water-heater-time; expected HH:MM (24-hour), e.g. 10:00" >&2
  exit 2
fi

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo ./upgrade.sh" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  if [[ ! -f "$LEGACY_CFG" ]]; then
    echo "Existing v2/v3 installation not found; use install.sh instead." >&2
    exit 1
  fi
  echo "Migrating legacy config.toml + credential files to v3 deye.env ..."
  python3 "$SRC_DIR/migrate_legacy_config.py" \
    --config "$LEGACY_CFG" \
    --credentials-dir "$LEGACY_CRED" \
    --output "$ENV_FILE"
  chown root:deyeopt "$ENV_FILE"
  chmod 640 "$ENV_FILE"
fi

# Add newly introduced v3.1 keys without changing any existing values/secrets.
python3 - "$ENV_FILE" "$SRC_DIR/.env.example" <<'PYMERGE'
from pathlib import Path
import sys
dst=Path(sys.argv[1]); tmpl=Path(sys.argv[2])
text=dst.read_text(encoding="utf-8")
existing=set()
for line in text.splitlines():
    st=line.strip()
    if st and not st.startswith("#") and "=" in st:
        existing.add(st.split("=",1)[0].replace("export ","").strip())
append=[]
for line in tmpl.read_text(encoding="utf-8").splitlines():
    st=line.strip()
    if not st or st.startswith("#") or "=" not in st:
        continue
    key=st.split("=",1)[0].replace("export ","").strip()
    if key not in existing:
        append.append(line); existing.add(key)
if append:
    text += ("" if text.endswith("\n") else "\n") + "\n# Added by v3.1 upgrade (defaults; review as desired)\n" + "\n".join(append) + "\n"
    dst.write_text(text,encoding="utf-8")
PYMERGE
chown root:deyeopt "$ENV_FILE"
chmod 640 "$ENV_FILE"

if [[ -n "$WATER_HEATER_TIME_OVERRIDE" ]]; then
  python3 - "$ENV_FILE" "$WATER_HEATER_TIME_OVERRIDE" <<'PYENV'
from pathlib import Path
import re, sys
p = Path(sys.argv[1])
value = sys.argv[2]
s = p.read_text(encoding="utf-8")
line = f'WATER_HEATER_TIME="{value}"'
if re.search(r'^WATER_HEATER_TIME=.*$', s, flags=re.M):
    s = re.sub(r'^WATER_HEATER_TIME=.*$', line, s, flags=re.M)
else:
    s += ("" if s.endswith("\n") else "\n") + line + "\n"
p.write_text(s, encoding="utf-8")
PYENV
  chown root:deyeopt "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  echo "Applied WATER_HEATER_TIME=$WATER_HEATER_TIME_OVERRIDE"
fi

systemctl stop deye-solar-optimizer.service 2>/dev/null || true
mkdir -p "$DEST/backups/$STAMP"
if [[ -d "$DEST" ]]; then
  find "$DEST" -maxdepth 1 -type f -exec cp -a {} "$DEST/backups/$STAMP/" \; 2>/dev/null || true
fi
mkdir -p "$ETC/backups/$STAMP"
cp -a "$ENV_FILE" "$ETC/backups/$STAMP/deye.env" 2>/dev/null || true
cp -a "$LEGACY_CFG" "$ETC/backups/$STAMP/config.toml" 2>/dev/null || true

install -d -m 755 -o root -g root "$DEST"
for f in controller.py status.py preflight.py window_probe.py control_probe.py profile_manager.py day_planner.py strategy_manager.py day_export.py discover.py migrate_legacy_config.py analytics_cli.py dashboard_server.py; do
  install -m 755 "$SRC_DIR/$f" "$DEST/$f"
done
for f in deye_api.py solar_forecast.py energy_strategy.py strategy_presets.py state_db.py config_loader.py analytics_engine.py; do
  install -m 644 "$SRC_DIR/$f" "$DEST/$f"
done
install -m 644 "$SRC_DIR/VERSION" "$DEST/VERSION"
install -m 644 "$SRC_DIR/dashboard.html" "$DEST/dashboard.html"

install -m 644 "$SRC_DIR/systemd/deye-solar-optimizer.service" /etc/systemd/system/deye-solar-optimizer.service
install -m 644 "$SRC_DIR/systemd/deye-solar-analytics.service" /etc/systemd/system/deye-solar-analytics.service
ln -sf "$DEST/status.py" /usr/local/bin/deyeopt-status
ln -sf "$DEST/preflight.py" /usr/local/bin/deyeopt-preflight
ln -sf "$DEST/discover.py" /usr/local/bin/deyeopt-discover
ln -sf "$DEST/control_probe.py" /usr/local/bin/deyeopt-control-probe
ln -sf "$DEST/window_probe.py" /usr/local/bin/deyeopt-window-probe
ln -sf "$DEST/profile_manager.py" /usr/local/bin/deyeopt-profile
ln -sf "$DEST/day_planner.py" /usr/local/bin/deyeopt-day-plan
ln -sf "$DEST/strategy_manager.py" /usr/local/bin/deyeopt-strategy
ln -sf "$DEST/day_export.py" /usr/local/bin/deye-day-export
ln -sf "$DEST/analytics_cli.py" /usr/local/bin/deyeopt-analytics

# Initialize/migrate local SQLite schema without Deye API calls.
runuser -u deyeopt -- env PYTHONPATH="$DEST" /usr/bin/python3 - <<'PYDB'
from config_loader import DEFAULT_ENV, load_config
from state_db import StateDB
cfg = load_config(DEFAULT_ENV)
db = StateDB(cfg['logging']['state_db'])
db.close()
PYDB

python3 -m py_compile "$DEST"/*.py
PYTHONPATH="$SRC_DIR" python3 -m unittest discover -s "$SRC_DIR/tests" -v

systemctl daemon-reload
systemctl enable deye-solar-analytics.service >/dev/null 2>&1 || true
systemctl restart deye-solar-optimizer.service
systemctl restart deye-solar-analytics.service
sleep 2
systemctl --no-pager --full status deye-solar-optimizer.service || true

echo
echo "Upgrade to v3.1.9 complete."
echo "Runtime config: $ENV_FILE"
echo "Code backup: $DEST/backups/$STAMP"
echo "Config backup: $ETC/backups/$STAMP"
echo "Quick checks:"
echo "  sudo deyeopt-status"
echo "  sudo deyeopt-strategy status"
echo "  sudo deye-day-export --hours 6"
echo "  sudo deyeopt-analytics"
echo "  dashboard: http://127.0.0.1:8787/"
