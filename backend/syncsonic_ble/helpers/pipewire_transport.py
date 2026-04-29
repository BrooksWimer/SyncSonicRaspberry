"""SyncSonic PipeWire transport manager (Slice 2 stereo elastic engine).

One ``pw_delay_filter`` process per speaker. Each filter exposes four
DSP ports (``input_FL`` / ``input_FR`` / ``output_FL`` / ``output_FR``)
and a Unix-socket control surface at
``/tmp/syncsonic-engine/<node_name>.sock``. Delay changes are sent over
the socket and applied with smooth fractional interpolation by the
filter; the route is no longer rebuilt for slider drags, which means a
delay change no longer causes a graph xrun. Bigger structural changes
(speaker disconnect/reconnect, codec renegotiation) still go through
the full ensure_route teardown/rebuild path.

Slice 2 contract
----------------
- ``ensure_route(mac, latency_ms=...)`` is the single entry point.
- If the route is healthy, the call is cheap: just sends ``set_delay``
  over the socket and updates volume.
- If the route is missing (first connect, after a process crash, after
  a teardown), a full build runs: ``_start_filter`` -> port wait ->
  ``_connect_route``.
- ``remove_route`` is the only path that terminates the filter process.
- Slice 3 will read the filter's queue depth and write rate_ppm via
  the same socket. The methods are scaffolded here as ``query_filter``
  and ``set_rate_ppm`` so the Coordinator can use them later without
  a second wire-format change.
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
from typing import Any, Dict, List, Optional

from syncsonic_ble.telemetry import EventType
from syncsonic_ble.telemetry.event_writer import emit
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
FILTER_SOURCE = BACKEND_ROOT / "tools" / "pw_delay_filter.c"
FILTER_BINARY = BACKEND_ROOT / "tools" / "pw_delay_filter"
SOCKET_DIR = Path("/tmp/syncsonic-engine")

# Tolerance for "the route's current delay is close enough to the
# requested delay that no socket round-trip is needed at all." The C
# filter slews at 4 samples/frame (~83 ppm); a 0.1 ms sub-tolerance
# means we send the socket command for every meaningful slider nudge
# but skip true no-ops.
DELAY_NOOP_TOLERANCE_MS = 0.1
SOCKET_TIMEOUT_SEC = 0.5


# ---------------------------------------------------------------------------
# Module-level helpers (callable without an instance)
# ---------------------------------------------------------------------------


def resolve_pipewire_output_name(mac: str) -> Optional[str]:
    mac = mac.upper()
    candidates = _candidate_output_prefixes(mac)
    result = subprocess.run(
        ["pactl", "list", "sinks", "short"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        sink_name = parts[1]
        for prefix in candidates:
            if sink_name.startswith(prefix):
                return sink_name
    return None


def _candidate_output_prefixes(mac: str) -> List[str]:
    token = mac.replace(":", "_")
    return [
        f"bluez_output.{token}.",
        f"bluez_output.{token}",
        f"bluez_sink.{token}",
    ]


def _filter_node_name(mac: str) -> str:
    return f"syncsonic-delay-{mac.replace(':', '_').lower()}"


def _socket_path_for_node(node_name: str) -> str:
    return str(SOCKET_DIR / f"{node_name}.sock")


def _legacy_filter_names(mac: str) -> List[str]:
    """Pre-Slice-2 mono-per-channel filter names. We pkill these on
    ensure_route to be sure no stale per-channel process is still
    holding the old port names.
    """
    base = mac.replace(":", "_").lower()
    return [
        f"syncsonic-delay-{base}-fl",
        f"syncsonic-delay-{base}-fr",
    ]


# ---------------------------------------------------------------------------
# PipeWireTransportManager
# ---------------------------------------------------------------------------


class PipeWireTransportManager:
    """One stereo elastic delay filter per speaker, controlled over a Unix socket."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._active_routes: Dict[str, Dict[str, Any]] = {}

    # -- public API ---------------------------------------------------------

    def ensure_route(
        self,
        mac: str,
        *,
        latency_ms: float = 0.0,
        left_percent: Optional[int] = None,
        right_percent: Optional[int] = None,
    ) -> bool:
        mac = mac.upper()
        sink_name = resolve_pipewire_output_name(mac)
        if not sink_name:
            log.warning("PipeWire transport sink not found for %s", mac)
            return False

        if not self._ensure_filter_binary():
            log.warning("PipeWire delay filter binary unavailable for %s", mac)
            return False

        target_delay_ms = max(0.0, float(latency_ms))
        target_left = self._normalize_percent(left_percent)
        target_right = self._normalize_percent(right_percent)
        node = _filter_node_name(mac)
        sock_path = _socket_path_for_node(node)

        # Fast path: route is already set up. Just push the delay over
        # the socket (cheap, no audio interruption).
        with self._lock:
            previous = self._active_routes.get(mac)
            if (
                previous
                and previous.get("sink") == sink_name
                and previous.get("node") == node
                and self._process_alive(previous.get("proc"))
            ):
                cur = float(previous.get("delay_ms", 0.0))
                if abs(cur - target_delay_ms) >= DELAY_NOOP_TOLERANCE_MS:
                    if self._send_set_delay(sock_path, target_delay_ms):
                        previous["delay_ms"] = target_delay_ms
                        log.info(
                            "PipeWire delay (live) %s: %.1f -> %.1f ms via socket",
                            mac, cur, target_delay_ms,
                        )
                        # Volume update is also non-disruptive
                        previous["left_percent"] = target_left
                        previous["right_percent"] = target_right
                        self._apply_sink_volume(sink_name, target_left, target_right)
                        return True
                    log.warning("Socket set_delay failed for %s; will rebuild route", mac)
                else:
                    # No-op delay; just refresh volume
                    previous["left_percent"] = target_left
                    previous["right_percent"] = target_right
                    self._apply_sink_volume(sink_name, target_left, target_right)
                    return True
            # Fallthrough: rebuild
            stale = self._active_routes.pop(mac, None)
            if stale:
                self._teardown_locked(stale)

        # Rebuild path: clean up legacy filter shapes and pre-Slice-2
        # routes that may still be linked, then start fresh.
        self._cleanup_legacy_filters(mac)
        self._disconnect_legacy_stage_route(mac, sink_name)
        self._disconnect_legacy_direct_route(sink_name)
        self._disconnect_legacy_per_channel_route(sink_name, mac)

        SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        proc = self._start_filter(node, target_delay_ms, sock_path)
        if proc is None:
            log.warning("Failed to launch stereo delay filter for %s", mac)
            return False

        if not self._stereo_ports_ready(node):
            self._terminate_process(proc)
            log.warning("Delay filter ports never appeared for %s", mac)
            return False

        if not self._connect_route(sink_name, node):
            self._disconnect_route(sink_name, node)
            self._terminate_process(proc)
            log.warning("Failed to wire PipeWire delay transport for %s", mac)
            return False

        self._apply_sink_volume(sink_name, target_left, target_right)

        with self._lock:
            self._active_routes[mac] = {
                "sink": sink_name,
                "node": node,
                "proc": proc,
                "socket_path": sock_path,
                "delay_ms": target_delay_ms,
                "left_percent": target_left,
                "right_percent": target_right,
            }

        log.info(
            "PipeWire delay transport established for %s via %s -> %s (delay %.1f ms)",
            mac, node, sink_name, target_delay_ms,
        )
        emit(EventType.ROUTE_CREATE, {
            "mac": mac,
            "sink": sink_name,
            "node": node,
            "delay_ms": target_delay_ms,
            "left_percent": target_left,
            "right_percent": target_right,
            "socket_path": sock_path,
        })
        return True

    def remove_route(self, mac: str) -> None:
        mac = mac.upper()
        with self._lock:
            route = self._active_routes.pop(mac, None)
        if route is None:
            self._disconnect_legacy_stage_route(mac, resolve_pipewire_output_name(mac) or "")
            self._cleanup_legacy_filters(mac)
            return

        self._teardown_locked(route)
        log.info("PipeWire delay transport removed for %s", mac)
        emit(EventType.ROUTE_TEARDOWN, {
            "mac": mac,
            "sink": route.get("sink", ""),
            "node": route.get("node", ""),
        })

    def query_filter(self, mac: str) -> Optional[Dict[str, Any]]:
        """Send ``query`` over the speaker's filter socket; return parsed dict."""
        mac = mac.upper()
        with self._lock:
            route = self._active_routes.get(mac)
            sock_path = route.get("socket_path") if route else None
        if not sock_path:
            return None
        return self._send_socket_command(sock_path, "query")

    def set_rate_ppm(self, mac: str, ppm: int) -> bool:
        """Send ``set_rate_ppm`` over the filter socket. Used by Slice 3."""
        mac = mac.upper()
        with self._lock:
            route = self._active_routes.get(mac)
            sock_path = route.get("socket_path") if route else None
        if not sock_path:
            return False
        resp = self._send_socket_command(sock_path, f"set_rate_ppm {int(ppm)}")
        return bool(resp and resp.get("ok"))

    # -- socket helpers -----------------------------------------------------

    def _send_set_delay(self, sock_path: str, delay_ms: float) -> bool:
        resp = self._send_socket_command(sock_path, f"set_delay {delay_ms:.3f}")
        return bool(resp and resp.get("ok"))

    def _send_socket_command(self, sock_path: str, line: str) -> Optional[Dict[str, Any]]:
        """Send one line over a Unix socket, read one JSON-line response."""
        if not os.path.exists(sock_path):
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(SOCKET_TIMEOUT_SEC)
                s.connect(sock_path)
                s.sendall((line + "\n").encode("ascii"))
                buf = b""
                while b"\n" not in buf and len(buf) < 1024:
                    chunk = s.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
        except (OSError, socket.timeout) as exc:
            log.debug("socket command %r to %s failed: %s", line, sock_path, exc)
            return None
        try:
            return json.loads(buf.decode("ascii", errors="replace").strip().split("\n")[0])
        except (ValueError, json.JSONDecodeError):
            return None

    # -- filter binary management ------------------------------------------

    def _ensure_filter_binary(self) -> bool:
        if not FILTER_SOURCE.exists():
            return False

        needs_build = (
            not FILTER_BINARY.exists()
            or FILTER_SOURCE.stat().st_mtime > FILTER_BINARY.stat().st_mtime
        )
        if not needs_build:
            return os.access(FILTER_BINARY, os.X_OK)

        # -pthread  for the new control thread / Unix-socket surface
        # -latomic  gcc on aarch64 emits libatomic calls for 8-byte
        #           atomic ops used by frames_in_total / frames_out_total
        compile_cmd = (
            f"gcc -O2 -Wall -Wextra -pthread -o {shlex.quote(str(FILTER_BINARY))} "
            f"{shlex.quote(str(FILTER_SOURCE))} "
            "$(/usr/bin/pkg-config --cflags --libs libpipewire-0.3) -latomic"
        )
        result = subprocess.run(
            ["/bin/sh", "-lc", compile_cmd],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning("Failed to compile pw_delay_filter: %s", (result.stderr or "").strip())
            return False
        return os.access(FILTER_BINARY, os.X_OK)

    def _start_filter(self, node_name: str, delay_ms: float, sock_path: str) -> Optional[subprocess.Popen]:
        self._kill_existing_filter(node_name)
        # Best-effort cleanup of stale socket from a prior crash.
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.debug("could not unlink stale socket %s: %s", sock_path, exc)
        try:
            proc = subprocess.Popen(
                [str(FILTER_BINARY), f"{delay_ms:.3f}", node_name, sock_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to start %s: %s", node_name, exc)
            return None
        return proc

    def _kill_existing_filter(self, node_name: str) -> None:
        subprocess.run(
            ["pkill", "-f", node_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _cleanup_legacy_filters(self, mac: str) -> None:
        """Kill any pre-Slice-2 mono-per-channel filter processes left over
        from a previous binary that still happen to be running."""
        for legacy in _legacy_filter_names(mac):
            subprocess.run(
                ["pkill", "-f", legacy],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _stereo_ports_ready(self, node_name: str, timeout_sec: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["pw-cli", "ls", "Port"],
                capture_output=True,
                text=True,
            )
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

    # -- pw-link wiring -----------------------------------------------------

    def _connect_route(self, sink_name: str, node: str) -> bool:
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
                    "pw-link failed for %s -> %s: %s",
                    source_port, target_port,
                    (result.stderr or "").strip(),
                )
                return False
        return True

    def _disconnect_route(self, sink_name: str, node: str) -> None:
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

    def _disconnect_legacy_direct_route(self, sink_name: str) -> None:
        for source_port, target_port in [
            ("virtual_out:monitor_FL", f"{sink_name}:playback_FL"),
            ("virtual_out:monitor_FR", f"{sink_name}:playback_FR"),
        ]:
            subprocess.run(
                ["pw-link", "-d", source_port, target_port],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _disconnect_legacy_per_channel_route(self, sink_name: str, mac: str) -> None:
        """Disconnect the pre-Slice-2 mono-per-channel layout if any
        such links are still alive in the graph (e.g. mid-deploy)."""
        node_fl, node_fr = _legacy_filter_names(mac)
        for source_port, target_port in [
            ("virtual_out:monitor_FL", f"{node_fl}:input"),
            (f"{node_fl}:output", f"{sink_name}:playback_FL"),
            ("virtual_out:monitor_FR", f"{node_fr}:input"),
            (f"{node_fr}:output", f"{sink_name}:playback_FR"),
        ]:
            subprocess.run(
                ["pw-link", "-d", source_port, target_port],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _disconnect_legacy_stage_route(self, mac: str, sink_name: str) -> None:
        stage_name = self._legacy_stage_name(mac)
        for source_port, target_port in [
            ("virtual_out:monitor_FL", f"{stage_name}:playback_FL"),
            ("virtual_out:monitor_FR", f"{stage_name}:playback_FR"),
            (f"{stage_name}:monitor_FL", f"{sink_name}:playback_FL"),
            (f"{stage_name}:monitor_FR", f"{sink_name}:playback_FR"),
        ]:
            subprocess.run(
                ["pw-link", "-d", source_port, target_port],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self._destroy_node(stage_name)

    def _destroy_node(self, node_name: str) -> None:
        node_id = self._find_node_id(node_name)
        if node_id is None:
            return
        subprocess.run(
            ["pw-cli", "destroy", str(node_id)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _find_node_id(self, node_name: str) -> Optional[int]:
        result = subprocess.run(
            ["pw-cli", "ls", "Node"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None

        current_id: Optional[int] = None
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if line.startswith("id "):
                try:
                    current_id = int(line.split(",", 1)[0].split()[1])
                except Exception:
                    current_id = None
                continue
            if current_id is None:
                continue
            if f'node.name = "{node_name}"' in line:
                return current_id
        return None

    # -- teardown / housekeeping -------------------------------------------

    def _teardown_locked(self, route: Dict[str, Any]) -> None:
        """Disconnect ports + terminate the filter process. The caller
        has already removed the entry from ``self._active_routes``."""
        sink = route.get("sink", "")
        node = route.get("node", "")
        if sink and node:
            self._disconnect_route(sink, node)
        proc = route.get("proc")
        # Try a graceful shutdown via the socket first; falls back to
        # SIGTERM/SIGKILL if the socket is dead.
        sock = route.get("socket_path")
        if sock and os.path.exists(sock):
            self._send_socket_command(sock, "quit")
        self._terminate_process(proc)
        # Best-effort socket cleanup; the C side also unlinks on clean exit.
        if sock:
            try:
                os.unlink(sock)
            except FileNotFoundError:
                pass
            except OSError:
                pass

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

    def _legacy_stage_name(self, mac: str) -> str:
        return f"syncsonic-stage-{mac.replace(':', '_').lower()}"

    def _normalize_percent(self, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        return int(max(0, min(150, int(value))))

    def _apply_sink_volume(
        self,
        sink_name: str,
        left_percent: Optional[int],
        right_percent: Optional[int],
    ) -> None:
        if left_percent is None or right_percent is None or not sink_name:
            return
        for _ in range(6):
            result = subprocess.run(
                ["pactl", "set-sink-volume", sink_name, f"{left_percent}%", f"{right_percent}%"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return
            time.sleep(0.05)
        log.warning(
            "Failed to set sink volume for %s -> %s%%/%s%%: %s",
            sink_name, left_percent, right_percent,
            (result.stderr or "").strip(),
        )


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_PIPEWIRE_TRANSPORT_MANAGER: Optional[PipeWireTransportManager] = None


def get_pipewire_transport_manager() -> PipeWireTransportManager:
    global _PIPEWIRE_TRANSPORT_MANAGER
    if _PIPEWIRE_TRANSPORT_MANAGER is None:
        _PIPEWIRE_TRANSPORT_MANAGER = PipeWireTransportManager()
    return _PIPEWIRE_TRANSPORT_MANAGER
