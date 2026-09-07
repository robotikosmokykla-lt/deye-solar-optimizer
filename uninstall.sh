#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run with sudo" >&2; exit 1; fi
systemctl disable --now deye-solar-optimizer.service 2>/dev/null || true
rm -f /etc/systemd/system/deye-solar-optimizer.service /usr/local/bin/deyeopt-status /usr/local/bin/deyeopt-control-probe /usr/local/bin/deyeopt-window-probe /usr/local/bin/deyeopt-profile /usr/local/bin/deyeopt-day-plan /usr/local/bin/deyeopt-strategy /usr/local/bin/deye-day-export /usr/local/bin/deyeopt-preflight
systemctl daemon-reload
rm -rf /opt/deye-solar-optimizer
cat <<'MSG'
Program removed. Configuration (.env), legacy credentials/config backups, and state were intentionally kept:
  /etc/deye-solar-optimizer
  /var/lib/deye-solar-optimizer
Delete them manually only if you explicitly want to erase history/secrets.
MSG
