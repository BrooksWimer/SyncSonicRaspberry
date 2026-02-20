#!/usr/bin/env bash
set -euxo pipefail

ENVFILE=/etc/default/syncsonic
[ -r "$ENVFILE" ] && source "$ENVFILE" || true

source /home/syncsonic/SyncSonicPi/backend/.venv/bin/activate
cd /home/syncsonic/SyncSonicPi/backend
exec python3 -u -m syncsonic_ble.main
