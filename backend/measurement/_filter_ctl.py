"""Direct filter-socket control: query and set delay on a single
syncsonic-delay-*.sock without going through the actuation manager.
Useful for diagnostics when we want to verify the C filter actually
applies what we think it does.

Usage:
  python _filter_ctl.py query <mac>
  python _filter_ctl.py set_delay <mac> <ms>          # ramp 100 ms
  python _filter_ctl.py set_delay <mac> <ms> <ramp>
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path


def sock_path_for(mac: str) -> Path:
    fname = "syncsonic-delay-" + mac.replace(":", "_").lower() + ".sock"
    return Path("/tmp/syncsonic-engine") / fname


def send(mac: str, line: str) -> str:
    sp = sock_path_for(mac)
    if not sp.exists():
        return f"NO SOCKET: {sp}"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect(str(sp))
        s.sendall((line + "\n").encode("ascii"))
        buf = b""
        while b"\n" not in buf and len(buf) < 4096:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.decode("ascii", errors="replace").strip()
    finally:
        s.close()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    mac = sys.argv[2]
    if cmd == "query":
        print(send(mac, "query"))
        return 0
    if cmd == "set_delay":
        if len(sys.argv) < 4:
            print(__doc__)
            return 1
        ms = float(sys.argv[3])
        ramp = float(sys.argv[4]) if len(sys.argv) >= 5 else 100.0
        print(send(mac, f"set_delay {ms} {ramp}"))
        return 0
    print(f"unknown cmd: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
