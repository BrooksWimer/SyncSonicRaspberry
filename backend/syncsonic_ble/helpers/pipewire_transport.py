from __future__ import annotations

import os
import shlex
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


class PipeWireTransportManager:
    """Maintains persistent PipeWire transport routes per output.

    The active transport path is a real custom pw_filter process per channel:
    - launch one mono delay filter for FL
    - launch one mono delay filter for FR
    - link virtual_out.monitor_* into the filter inputs
    - link the filter outputs to the target speaker sink playback ports

    This keeps audio in a real processing node with an owned delay line, which
    is the transport primitive we validated live.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._active_routes: Dict[str, Dict[str, Any]] = {}

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
        node_fl = self._filter_name(mac, "fl")
        node_fr = self._filter_name(mac, "fr")

        with self._lock:
            previous = self._active_routes.get(mac)
            if (
                previous
                and previous.get("sink") == sink_name
                and abs(float(previous.get("delay_ms", 0.0)) - target_delay_ms) < 0.5
                and self._process_alive(previous.get("proc_fl"))
                and self._process_alive(previous.get("proc_fr"))
                and self._ports_ready(node_fl)
                and self._ports_ready(node_fr)
            ):
                previous["left_percent"] = target_left
                previous["right_percent"] = target_right
                self._apply_sink_volume(sink_name, target_left, target_right)
                return True

            if previous:
                self._disconnect_route(previous["sink"], previous["node_fl"], previous["node_fr"])
                self._terminate_process(previous.get("proc_fl"))
                self._terminate_process(previous.get("proc_fr"))

        self._disconnect_legacy_stage_route(mac, sink_name)
        self._disconnect_legacy_direct_route(sink_name)

        proc_fl = self._start_filter(node_fl, target_delay_ms)
        proc_fr = self._start_filter(node_fr, target_delay_ms)
        if proc_fl is None or proc_fr is None:
            self._terminate_process(proc_fl)
            self._terminate_process(proc_fr)
            log.warning("Failed to launch PipeWire delay filters for %s", mac)
            return False

        if not self._ports_ready(node_fl) or not self._ports_ready(node_fr):
            self._terminate_process(proc_fl)
            self._terminate_process(proc_fr)
            log.warning("Delay filter ports never appeared for %s", mac)
            return False

        if not self._connect_route(sink_name, node_fl, node_fr):
            self._disconnect_route(sink_name, node_fl, node_fr)
            self._terminate_process(proc_fl)
            self._terminate_process(proc_fr)
            log.warning("Failed to wire PipeWire delay transport for %s", mac)
            return False

        self._apply_sink_volume(sink_name, target_left, target_right)

        with self._lock:
            self._active_routes[mac] = {
                "sink": sink_name,
                "node_fl": node_fl,
                "node_fr": node_fr,
                "proc_fl": proc_fl,
                "proc_fr": proc_fr,
                "delay_ms": target_delay_ms,
                "left_percent": target_left,
                "right_percent": target_right,
            }

        log.info(
            "PipeWire delay transport established for %s via %s/%s -> %s (delay %.1f ms)",
            mac,
            node_fl,
            node_fr,
            sink_name,
            target_delay_ms,
        )
        emit(EventType.ROUTE_CREATE, {
            "mac": mac,
            "sink": sink_name,
            "node_fl": node_fl,
            "node_fr": node_fr,
            "delay_ms": target_delay_ms,
            "left_percent": target_left,
            "right_percent": target_right,
        })
        return True

    def remove_route(self, mac: str) -> None:
        mac = mac.upper()
        with self._lock:
            route = self._active_routes.pop(mac, None)
        if route is None:
            self._disconnect_legacy_stage_route(mac, resolve_pipewire_output_name(mac) or "")
            return

        self._disconnect_route(route["sink"], route["node_fl"], route["node_fr"])
        self._terminate_process(route.get("proc_fl"))
        self._terminate_process(route.get("proc_fr"))
        log.info("PipeWire delay transport removed for %s", mac)
        emit(EventType.ROUTE_TEARDOWN, {
            "mac": mac,
            "sink": route.get("sink", ""),
        })

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
            f"gcc -O2 -Wall -Wextra -o {shlex.quote(str(FILTER_BINARY))} "
            f"{shlex.quote(str(FILTER_SOURCE))} "
            "$(/usr/bin/pkg-config --cflags --libs libpipewire-0.3)"
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

    def _start_filter(self, node_name: str, delay_ms: float) -> Optional[subprocess.Popen]:
        self._kill_existing_filter(node_name)
        try:
            proc = subprocess.Popen(
                [str(FILTER_BINARY), f"{delay_ms:.3f}", node_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
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

    def _ports_ready(self, node_name: str, timeout_sec: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["pw-cli", "ls", "Port"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                text = result.stdout
                if (
                    f'port.alias = "{node_name}:input"' in text
                    and f'port.alias = "{node_name}:output"' in text
                ):
                    return True
            time.sleep(0.05)
        return False

    def _connect_route(self, sink_name: str, node_fl: str, node_fr: str) -> bool:
        link_pairs = [
            ("virtual_out:monitor_FL", f"{node_fl}:input"),
            (f"{node_fl}:output", f"{sink_name}:playback_FL"),
            ("virtual_out:monitor_FR", f"{node_fr}:input"),
            (f"{node_fr}:output", f"{sink_name}:playback_FR"),
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
                    source_port,
                    target_port,
                    (result.stderr or "").strip(),
                )
                return False
        return True

    def _disconnect_route(self, sink_name: str, node_fl: str, node_fr: str) -> None:
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

    def _filter_name(self, mac: str, channel: str) -> str:
        return f"syncsonic-delay-{mac.replace(':', '_').lower()}-{channel}"

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
            sink_name,
            left_percent,
            right_percent,
            (result.stderr or "").strip(),
        )


_PIPEWIRE_TRANSPORT_MANAGER: Optional[PipeWireTransportManager] = None


def get_pipewire_transport_manager() -> PipeWireTransportManager:
    global _PIPEWIRE_TRANSPORT_MANAGER
    if _PIPEWIRE_TRANSPORT_MANAGER is None:
        _PIPEWIRE_TRANSPORT_MANAGER = PipeWireTransportManager()
    return _PIPEWIRE_TRANSPORT_MANAGER
