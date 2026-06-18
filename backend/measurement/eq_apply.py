"""Enable or disable per-speaker EQ filters on the live PipeWire graph."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ["XDG_RUNTIME_DIR"] = os.environ.get("RUNTIME_DIRECTORY", "/run/syncsonic")
os.environ["PULSE_SERVER"] = "unix:/run/syncsonic/pulse/native"
os.environ["PULSE_SYSTEM_BUS"] = "1"
os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={os.environ['XDG_RUNTIME_DIR']}/bus"

from syncsonic_ble.helpers.pipewire_eq_transport import (  # noqa: E402
    get_pipewire_eq_transport_manager,
)

DEFAULT_MACS = (
    "F4:6A:DD:D4:F3:C8",
    "28:FA:19:B6:0E:3B",
    "2C:FD:B4:69:46:0A",
)


def _connected_bluez_macs() -> list[str]:
    result = subprocess.run(["pactl", "list", "sinks", "short"], capture_output=True, text=True)
    macs: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("bluez_output."):
            sink = parts[1]
            body = sink.removeprefix("bluez_output.").removesuffix(".1")
            mac = ":".join(body.split("_")).upper()
            macs.append(mac)
    return macs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mac", action="append", default=[], help="Speaker MAC (repeatable)")
    parser.add_argument("--all-connected", action="store_true", help="All connected BT sinks")
    parser.add_argument("--disable", action="store_true", help="Remove EQ instead of enabling")
    args = parser.parse_args(argv)

    macs = [m.upper() for m in args.mac]
    if args.all_connected:
        macs.extend(_connected_bluez_macs())
    if not macs:
        macs = list(DEFAULT_MACS)

    mgr = get_pipewire_eq_transport_manager()
    report: dict[str, object] = {"action": "disable" if args.disable else "enable", "speakers": {}}
    ok = True
    for mac in dict.fromkeys(macs):
        if args.disable:
            mgr.remove_eq(mac)
            report["speakers"][mac] = {"ok": True, "enabled": False}
            continue
        success = mgr.ensure_eq(mac, enabled=True)
        query = mgr.query_eq(mac) or {}
        report["speakers"][mac] = {
            "ok": success,
            "enabled": success,
            "profile_band_count": query.get("profile_band_count"),
            "profile_version": query.get("profile_version"),
        }
        ok = ok and success

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
