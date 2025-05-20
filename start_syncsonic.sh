#!/usr/bin/env bash
set -euxo pipefail

ENVFILE=/etc/default/syncsonic

# 1) Load whatever was already in /etc/default/syncsonic
[ -r "$ENVFILE" ] && source "$ENVFILE" || true

# 3) Reset adapters (this writes "export RESERVED_HCI=…" into $ENVFILE)
"/home/syncsonic/SyncSonicPi/reset_bt_adapters.sh"

# 4) **Re‑load** the env file, so RESERVED_HCI is now in our shell
source "$ENVFILE"

# 5) Activate venv & launch
source /home/syncsonic/venv/bin/activate
cd /home/syncsonic/SyncSonicPi
exec python -u -m syncsonic_ble.main 