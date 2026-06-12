"""PipeWire per-speaker EQ filter wiring.

EQ is opt-in per speaker. The helper only creates a route when callers
explicitly pass ``enabled=True``; default behavior is off and leaves the
existing delay transport untouched.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

from syncsonic_ble.helpers.pipewire_transport import resolve_pipewire_output_name
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
FILTER_SOURCE = BACKEND_ROOT / "tools" / "pw_eq_filter.c"
FILTER_BINARY = BACKEND_ROOT / "tools" / "pw_eq_filter"
SOCKET_DIR = Path("/tmp/syncsonic-engine")
SOCKET_TIMEOUT_SEC = 0.5


def _eq_node_name(mac: str) -> str:
    return f"syncsonic-eq-{mac.replace(':', '_').lower()}"


def _socket_path_for_node(node_name: str) -> str:
    return str(SOCKET_DIR / f"{node_name}.sock")


class PipeWireEqTransportManager:
    """Manage optional per-speaker ``pw_eq_filter`` nodes."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._active_routes: Dict[str, Dict[str, Any]] = {}

    def ensure_eq(self, mac: str, *, enabled: bool = False) -> bool:
        mac = mac.upper()
        if not enabled:
            self.remove_eq(mac)
            return True

        sink_name = resolve_pipewire_output_name(mac)
        if not sink_name:
            log.warning("PipeWire EQ sink not found for %s", mac)
            return False
        if not self._ensure_filter_binary():
            log.warning("PipeWire EQ filter binary unavailable for %s", mac)
            return False

        node = _eq_node_name(mac)
        sock_path = _socket_path_for_node(node)
        with self._lock:
            previous = self._active_routes.get(mac)
            if (
                previous
                and previous.get("sink") == sink_name
                and previous.get("node") == node
                and self._process_alive(previous.get("proc"))
            ):
                self.reload_profile(mac)
                return True
            stale = self._active_routes.pop(mac, None)
            if stale:
                self._teardown_locked(stale)

        SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        proc = self._start_filter(mac, node, sock_path)
        if proc is None:
            return False
        if not self._stereo_ports_ready(node):
            self._terminate_process(proc)
            return False
        if not self._connect_eq_route(sink_name, node):
            self._disconnect_eq_route(sink_name, node)
            self._terminate_process(proc)
            return False

        with self._lock:
            self._active_routes[mac] = {
                "sink": sink_name,
                "node": node,
                "proc": proc,
                "socket_path": sock_path,
            }
        log.info("PipeWire EQ enabled for %s via %s -> %s", mac, node, sink_name)
        return True

    def remove_eq(self, mac: str) -> None:
        mac = mac.upper()
        with self._lock:
            route = self._active_routes.pop(mac, None)
        if route:
            self._teardown_locked(route)

    def reload_profile(self, mac: str) -> bool:
        mac = mac.upper()
        with self._lock:
            route = self._active_routes.get(mac)
            sock_path = route.get("socket_path") if route else None
        if not sock_path:
            return False
        resp = self._send_socket_command(sock_path, "reload_profile")
        return bool(resp and resp.get("ok"))

    def query_eq(self, mac: str) -> Optional[Dict[str, Any]]:
        mac = mac.upper()
        with self._lock:
            route = self._active_routes.get(mac)
            sock_path = route.get("socket_path") if route else None
        if not sock_path:
            return None
        return self._send_socket_command(sock_path, "query")

    def _ensure_filter_binary(self) -> bool:
        if not FILTER_SOURCE.exists():
            return False
        needs_build = (
            not FILTER_BINARY.exists()
            or FILTER_SOURCE.stat().st_mtime > FILTER_BINARY.stat().st_mtime
        )
        if not needs_build:
            return os.access(FILTER_BINARY, os.X_OK)

        compile_cmd = (
            f"gcc -O2 -Wall -Wextra -pthread -o {shlex.quote(str(FILTER_BINARY))} "
            f"{shlex.quote(str(FILTER_SOURCE))} "
            "$(/usr/bin/pkg-config --cflags --libs libpipewire-0.3) -latomic -lm"
        )
        result = subprocess.run(["/bin/sh", "-lc", compile_cmd], capture_output=True, text=True)
        if result.returncode != 0:
            log.warning("Failed to compile pw_eq_filter: %s", (result.stderr or "").strip())
            return False
        return os.access(FILTER_BINARY, os.X_OK)

    def _start_filter(self, mac: str, node_name: str, sock_path: str) -> Optional[subprocess.Popen]:
        self._kill_existing_filter(node_name)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.debug("could not unlink stale EQ socket %s: %s", sock_path, exc)
        try:
            return subprocess.Popen(
                [str(FILTER_BINARY), mac, node_name, sock_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to start %s: %s", node_name, exc)
            return None

    def _kill_existing_filter(self, node_name: str) -> None:
        subprocess.run(
            ["pkill", "-f", node_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _stereo_ports_ready(self, node_name: str, timeout_sec: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            result = subprocess.run(["pw-cli", "ls", "Port"], capture_output=True, text=True)
            if result.returncode == 0:
                text = result.stdout
                needed = (
                    f'port.alias = "{node_name}:input_FL"',
                    f'port.alias = "{node_name}:input_FR"',
                    f'port.alias = "{node_name}:output_FL"',
                    f'port.alias = "{node_name}:output_FR"',
                )
                if all(snippet in text for snippet in needed):
                    return True
            time.sleep(0.05)
        return False

    def _connect_eq_route(self, sink_name: str, node: str) -> bool:
        # CRITICAL PORT LINKAGE - no graph cycle:
        #   virtual_out:monitor_FL -> <eq>:input_FL
        #   virtual_out:monitor_FR -> <eq>:input_FR
        #   <eq>:output_FL        -> <speaker sink>:playback_FL
        #   <eq>:output_FR        -> <speaker sink>:playback_FR
        #
        # The EQ node reads only from the shared virtual_out monitor and
        # writes only to the physical speaker sink. It never links any
        # speaker monitor back into virtual_out, and it never links its
        # own output back to its input, so the route is acyclic.
        link_pairs = [
            ("virtual_out:monitor_FL", f"{node}:input_FL"),
            ("virtual_out:monitor_FR", f"{node}:input_FR"),
            (f"{node}:output_FL", f"{sink_name}:playback_FL"),
            (f"{node}:output_FR", f"{sink_name}:playback_FR"),
        ]
        for source_port, target_port in link_pairs:
            result = subprocess.run(
                ["pw-link", "-L", "-P", source_port, target_port],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                log.warning(
                    "EQ pw-link failed for %s -> %s: %s",
                    source_port,
                    target_port,
                    (result.stderr or "").strip(),
                )
                return False
        return True

    def _disconnect_eq_route(self, sink_name: str, node: str) -> None:
        for source_port, target_port in [
            ("virtual_out:monitor_FL", f"{node}:input_FL"),
            ("virtual_out:monitor_FR", f"{node}:input_FR"),
            (f"{node}:output_FL", f"{sink_name}:playback_FL"),
            (f"{node}:output_FR", f"{sink_name}:playback_FR"),
        ]:
            subprocess.run(
                ["pw-link", "-d", source_port, target_port],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _teardown_locked(self, route: Dict[str, Any]) -> None:
        sink = route.get("sink", "")
        node = route.get("node", "")
        if sink and node:
            self._disconnect_eq_route(sink, node)
        sock = route.get("socket_path")
        if sock and os.path.exists(sock):
            self._send_socket_command(sock, "quit")
        self._terminate_process(route.get("proc"))
        if sock:
            try:
                os.unlink(sock)
            except OSError:
                pass

    def _send_socket_command(self, sock_path: str, line: str) -> Optional[Dict[str, Any]]:
        if not os.path.exists(sock_path):
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(SOCKET_TIMEOUT_SEC)
                s.connect(sock_path)
                s.sendall((line + "\n").encode("ascii"))
                buf = b""
                while b"\n" not in buf and len(buf) < 4096:
                    chunk = s.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
        except (OSError, socket.timeout) as exc:
            log.debug("EQ socket command %r to %s failed: %s", line, sock_path, exc)
            return None
        try:
            return json.loads(buf.decode("ascii", errors="replace").strip().split("\n")[0])
        except (ValueError, json.JSONDecodeError):
            return None

    def _process_alive(self, proc: Any) -> bool:
        return proc is not None and proc.poll() is None

    def _terminate_process(self, proc: Any) -> None:
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


_PIPEWIRE_EQ_TRANSPORT_MANAGER: Optional[PipeWireEqTransportManager] = None


def get_pipewire_eq_transport_manager() -> PipeWireEqTransportManager:
    global _PIPEWIRE_EQ_TRANSPORT_MANAGER
    if _PIPEWIRE_EQ_TRANSPORT_MANAGER is None:
        _PIPEWIRE_EQ_TRANSPORT_MANAGER = PipeWireEqTransportManager()
    return _PIPEWIRE_EQ_TRANSPORT_MANAGER
