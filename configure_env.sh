#!/usr/bin/env bash
set -euo pipefail
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/etc/deye-solar-optimizer/deye.env
if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo ./configure_env.sh" >&2
  exit 1
fi
install -d -m 750 -o root -g deyeopt /etc/deye-solar-optimizer
if [[ ! -f "$DEST" ]]; then
  install -m 640 -o root -g deyeopt "$SRC_DIR/.env.example" "$DEST"
fi
echo "Edit: $DEST"
echo "Recommended: sudoedit $DEST"
