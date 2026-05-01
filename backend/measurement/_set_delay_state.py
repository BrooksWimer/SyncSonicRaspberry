"""Atomically write control_state.json with a single MAC's user_delay.

Direct edit avoids the PowerShell-via-SSH quoting nightmare around
nested JSON.
"""
import json
import os
import sys
import tempfile

PATH = "/tmp/syncsonic_pipewire/control_state.json"

if len(sys.argv) != 3:
    print("usage: _set_delay_state.py <MAC> <user_delay_ms>")
    sys.exit(1)

mac = sys.argv[1].upper()
user_delay_ms = float(sys.argv[2])

try:
    with open(PATH, "r") as f:
        state = json.load(f)
except (OSError, ValueError):
    state = {"outputs": {}, "schema": 1}

outputs = state.setdefault("outputs", {})
entry = outputs.setdefault(mac, {})
entry["active"] = True
entry["delay_ms"] = user_delay_ms
entry["left_percent"] = entry.get("left_percent", 100)
entry["right_percent"] = entry.get("right_percent", 100)
entry["mode"] = "manual"
entry["rate_ppm"] = 0.0
state["schema"] = state.get("schema", 1)

dirpath = os.path.dirname(PATH) or "."
fd, tmp = tempfile.mkstemp(prefix=".control_state.", dir=dirpath)
with os.fdopen(fd, "w") as f:
    json.dump(state, f)
os.replace(tmp, PATH)

with open(PATH, "r") as f:
    print(f.read())
