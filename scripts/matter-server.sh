#!/usr/bin/env bash
# Runs the open-source Matter controller that house-agent talks to over
# WebSocket (ws://127.0.0.1:5580/ws). Install once with:
#   uv tool install --python 3.12 "python-matter-server[server]"
# (It lives in its own Python 3.12 environment because its native CHIP
# wheels do not support this project's Python yet.)
#
# Keep this running alongside house-agent -- as the systemd user service in
# scripts/matter-server.service, ideally. Device pairing and the Matter
# fabric credentials live in ~/.matter_server; the guard below snapshots
# them and puts them back if they ever go missing, but back that directory
# up as well.
set -euo pipefail

BIN="$(command -v matter-server || echo "$HOME/.local/bin/matter-server")"
STORAGE="${MATTER_STORAGE:-$HOME/.matter_server}"
GUARD="$(dirname "$(readlink -f "$0")")/matter-storage-guard.py"
mkdir -p "$STORAGE"

# One controller per storage folder. Two matter-servers sharing it (say, the
# service plus one started by hand in a terminal) both write the same file,
# and the second cannot even bind the port -- it just corrupts the first.
exec 9>"$STORAGE/.lock"
if ! flock -n 9; then
  echo "matter-server is already running against $STORAGE (systemctl --user status matter-server)" >&2
  exit 1
fi

# If the pairing list is empty but a snapshot has devices, restore it before
# the server starts and quietly declares that nothing is paired.
python3 "$GUARD" restore-if-empty "$STORAGE"
python3 "$GUARD" snapshot "$STORAGE"

# With a Bluetooth adapter the controller commissions brand-new devices
# straight out of the box. house-agent sets MATTER_BLUETOOTH_ADAPTER (the
# hci index, normally 0) from [matter] bluetooth / bluetooth_adapter in
# config.toml; export it yourself when running this script by hand.
BLUETOOTH=()
if [[ -n "${MATTER_BLUETOOTH_ADAPTER:-}" ]]; then
  BLUETOOTH=(--bluetooth-adapter "$MATTER_BLUETOOTH_ADAPTER")
fi

exec "$BIN" \
  --storage-path "$STORAGE" \
  --listen-address 127.0.0.1 \
  --port 5580 \
  --log-level info \
  "${BLUETOOTH[@]}"
