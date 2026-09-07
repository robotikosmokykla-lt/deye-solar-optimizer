#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_SRC=""
START_NOW=0

usage() {
  cat <<USAGE
Usage: sudo ./install.sh [--env-file /path/to/deye.env] [--start]

Installs to:
  /opt/deye-solar-optimizer
  /etc/deye-solar-optimizer/deye.env
  /var/lib/deye-solar-optimizer
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_SRC="$2"; shift 2 ;;
    --start) START_NOW=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Run as root, e.g. sudo ./install.sh ..." >&2
  exit 1
fi

python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3,11) else 1)
PY

if ! id deyeopt >/dev/null 2>&1; then
  useradd --system --home /var/lib/deye-solar-optimizer --shell /usr/sbin/nologin deyeopt
fi

install -d -m 755 -o root -g root /opt/deye-solar-optimizer
install -d -m 750 -o root -g deyeopt /etc/deye-solar-optimizer
install -d -m 750 -o deyeopt -g deyeopt /var/lib/deye-solar-optimizer
install -d -m 750 -o deyeopt -g deyeopt /var/lib/deye-solar-optimizer/exports

for f in controller.py status.py preflight.py window_probe.py control_probe.py profile_manager.py day_planner.py strategy_manager.py day_export.py discover.py migrate_legacy_config.py analytics_cli.py dashboard_server.py; do
  install -m 755 "$SRC_DIR/$f" "/opt/deye-solar-optimizer/$f"
done
for f in deye_api.py solar_forecast.py energy_strategy.py strategy_presets.py state_db.py config_loader.py analytics_engine.py; do
  install -m 644 "$SRC_DIR/$f" "/opt/deye-solar-optimizer/$f"
done
install -m 644 "$SRC_DIR/VERSION" /opt/deye-solar-optimizer/VERSION
install -m 644 "$SRC_DIR/dashboard.html" /opt/deye-solar-optimizer/dashboard.html

if [[ -n "$ENV_SRC" ]]; then
  install -m 640 -o root -g deyeopt "$ENV_SRC" /etc/deye-solar-optimizer/deye.env
elif [[ ! -f /etc/deye-solar-optimizer/deye.env ]]; then
  install -m 640 -o root -g deyeopt "$SRC_DIR/.env.example" /etc/deye-solar-optimizer/deye.env
  echo "Installed template /etc/deye-solar-optimizer/deye.env; fill credentials/site values before starting."
else
  echo "Keeping existing /etc/deye-solar-optimizer/deye.env"
fi

install -m 644 "$SRC_DIR/systemd/deye-solar-optimizer.service" /etc/systemd/system/deye-solar-optimizer.service
install -m 644 "$SRC_DIR/systemd/deye-solar-analytics.service" /etc/systemd/system/deye-solar-analytics.service
ln -sf /opt/deye-solar-optimizer/status.py /usr/local/bin/deyeopt-status
ln -sf /opt/deye-solar-optimizer/preflight.py /usr/local/bin/deyeopt-preflight
ln -sf /opt/deye-solar-optimizer/discover.py /usr/local/bin/deyeopt-discover
ln -sf /opt/deye-solar-optimizer/control_probe.py /usr/local/bin/deyeopt-control-probe
ln -sf /opt/deye-solar-optimizer/window_probe.py /usr/local/bin/deyeopt-window-probe
ln -sf /opt/deye-solar-optimizer/profile_manager.py /usr/local/bin/deyeopt-profile
ln -sf /opt/deye-solar-optimizer/day_planner.py /usr/local/bin/deyeopt-day-plan
ln -sf /opt/deye-solar-optimizer/strategy_manager.py /usr/local/bin/deyeopt-strategy
ln -sf /opt/deye-solar-optimizer/day_export.py /usr/local/bin/deye-day-export
ln -sf /opt/deye-solar-optimizer/analytics_cli.py /usr/local/bin/deyeopt-analytics

# Local DB initialization only; no network/control call.
runuser -u deyeopt -- env PYTHONPATH=/opt/deye-solar-optimizer /usr/bin/python3 - <<'PYDB'
from config_loader import DEFAULT_ENV, load_config
from state_db import StateDB
cfg = load_config(DEFAULT_ENV)
db = StateDB(cfg['logging']['state_db'])
db.close()
print(f"Initialized state DB: {cfg['logging']['state_db']}")
PYDB

systemctl daemon-reload
systemctl enable deye-solar-optimizer.service
systemctl enable deye-solar-analytics.service
python3 -m py_compile /opt/deye-solar-optimizer/*.py
PYTHONPATH="$SRC_DIR" python3 -m unittest discover -s "$SRC_DIR/tests" -v

if [[ $START_NOW -eq 1 ]]; then
  systemctl restart deye-solar-optimizer.service
  systemctl restart deye-solar-analytics.service
  sleep 2
  systemctl --no-pager --full status deye-solar-optimizer.service || true
else
  echo
  echo "Installed but not started. Recommended:"
  echo "  sudoedit /etc/deye-solar-optimizer/deye.env"
  echo "  sudo deyeopt-preflight  # or python3 /opt/deye-solar-optimizer/preflight.py"
  echo "  sudo systemctl restart deye-solar-optimizer"
  echo "  sudo deyeopt-status"
  echo "  sudo deyeopt-analytics"
  echo "  dashboard: http://127.0.0.1:8787/"
fi
