from __future__ import annotations

import os
import json
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, List, Optional

from syncsonic_ble.utils.logging_conf import get_logger
from syncsonic_ble.helpers.device_labels import format_device_label

log = get_logger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
FILTER_SOURCE = BACKEND_ROOT / "tools" / "pw_delay_filter.c"
FILTER_BINARY = BACKEND_ROOT / "tools" / "pw_delay_filter"
DSP_STATE_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
DSP_CALLBACK_STATE_PATH = os.path.join(DSP_STATE_DIR, "dsp_callback_state.json")
DSP_WARN_LATE_RATIO = float(os.environ.get("SYNCSONIC_DSP_WARN_LATE_RATIO", "0.05"))
DSP_WARN_GAP_MS = float(os.environ.get("SYNCSONIC_DSP_WARN_GAP_MS", "40.0"))
DSP_LOG_HEARTBEAT_SEC = float(os.environ.get("SYNCSONIC_DSP_LOG_HEARTBEAT_SEC", "0"))


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
        self._dsp_metrics: Dict[str, Dict[str, Any]] = {}
        self._dsp_warning_state: Dict[str, bool] = {}
        self._last_dsp_heartbeat_ts: Dict[str, float] = {}

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
            log.warning("PipeWire transport sink not found for %s", format_device_label(mac))
            return False

        if not self._ensure_filter_binary():
            log.warning("PipeWire delay filter binary unavailable for %s", format_device_label(mac))
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
            self._dsp_metrics.setdefault(mac, {"channels": {}, "updated_at_unix": 0.0})

        log.info(
            "PipeWire delay transport established for %s via %s/%s -> %s (delay %.1f ms)",
            format_device_label(mac),
            node_fl,
            node_fr,
            sink_name,
            target_delay_ms,
        )
        return True

    def remove_route(self, mac: str) -> None:
        mac = mac.upper()
        with self._lock:
            route = self._active_routes.pop(mac, None)
            self._dsp_metrics.pop(mac, None)
            self._last_dsp_heartbeat_ts.pop(mac, None)
            for key in list(self._dsp_warning_state.keys()):
                if key.startswith(f"{mac}:"):
                    self._dsp_warning_state.pop(key, None)
        if route is None:
            self._disconnect_legacy_stage_route(mac, resolve_pipewire_output_name(mac) or "")
            return

        self._disconnect_route(route["sink"], route["node_fl"], route["node_fr"])
        self._terminate_process(route.get("proc_fl"))
        self._terminate_process(route.get("proc_fr"))
        log.info("PipeWire delay transport removed for %s", format_device_label(mac))
        self._persist_dsp_state()

    def get_dsp_telemetry_snapshot(self, mac: str) -> Dict[str, Any]:
        mac = mac.upper()
        with self._lock:
            metrics = self._dsp_metrics.get(mac, {})
            return dict(metrics) if isinstance(metrics, dict) else {}

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
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            log.warning("Failed to start %s: %s", node_name, exc)
            return None
        channel = "fl" if node_name.endswith("-fl") else "fr" if node_name.endswith("-fr") else "mono"
        monitor = Thread(
            target=self._monitor_filter_stderr,
            args=(proc, node_name, channel),
            daemon=True,
        )
        monitor.start()
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
        try:
            if proc.stderr:
                proc.stderr.close()
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

    def _monitor_filter_stderr(self, proc: subprocess.Popen, node_name: str, channel: str) -> None:
        if proc.stderr is None:
            return
        while True:
            try:
                line = proc.stderr.readline()
            except ValueError:
                # Stderr may already be closed while the process is being
                # replaced/terminated; treat that as a normal shutdown path.
                break
            if line == "":
                break
            text = line.strip()
            if not text:
                continue
            if text.startswith("PW_DSP_EVT "):
                payload = self._parse_kv_line(text, "PW_DSP_EVT ")
                self._handle_dsp_event(node_name=node_name, channel=channel, payload=payload)
                continue
            if text.startswith("PW_DSP "):
                payload = self._parse_kv_line(text, "PW_DSP ")
                self._handle_dsp_heartbeat(node_name=node_name, channel=channel, payload=payload)
                continue
            if "state error" in text:
                log.warning("PipeWire delay filter runtime error (%s): %s", node_name, text)

    def _handle_dsp_event(self, *, node_name: str, channel: str, payload: Dict[str, str]) -> None:
        mac = self._mac_from_node_name(node_name)
        if not mac:
            return
        event_type = payload.get("type", "unknown")
        gap_ms = self._to_float(payload.get("gap_ms"))
        expected_ms = self._to_float(payload.get("expected_ms"))
        cycles = self._to_int(payload.get("cycles"))
        now_unix = time.time()
        with self._lock:
            entry = self._dsp_metrics.setdefault(mac, {"channels": {}, "updated_at_unix": 0.0})
            channel_entry = dict(entry.get("channels", {}).get(channel, {}))
            channel_entry["last_event_type"] = event_type
            channel_entry["last_event_gap_ms"] = gap_ms
            channel_entry["last_event_expected_ms"] = expected_ms
            channel_entry["last_event_cycles"] = cycles
            channel_entry["last_event_at_unix"] = now_unix
            channels = dict(entry.get("channels", {}))
            channels[channel] = channel_entry
            entry["channels"] = channels
            entry["updated_at_unix"] = now_unix
        self._persist_dsp_state()
        log.warning(
            "[DSP_EVT] speaker=%s channel=%s type=%s gap_ms=%.3f expected_ms=%.3f cycles=%d",
            format_device_label(mac),
            channel,
            event_type,
            gap_ms,
            expected_ms,
            cycles,
        )

    def _handle_dsp_heartbeat(self, *, node_name: str, channel: str, payload: Dict[str, str]) -> None:
        mac = self._mac_from_node_name(node_name)
        if not mac:
            return
        now_unix = time.time()
        late_ratio = self._to_float(payload.get("late_ratio"))
        gap_ms = self._to_float(payload.get("gap_ms"))
        expected_ms = self._to_float(payload.get("expected_ms"))
        avg_abs = self._to_float(payload.get("avg_abs"))
        peak_abs = self._to_float(payload.get("peak_abs"))
        cycles = self._to_int(payload.get("cycles"))
        late = self._to_int(payload.get("late"))
        silence_ratio = self._to_float(payload.get("silence_ratio"))
        with self._lock:
            entry = self._dsp_metrics.setdefault(mac, {"channels": {}, "updated_at_unix": 0.0})
            channels = dict(entry.get("channels", {}))
            channel_entry = dict(channels.get(channel, {}))
            channel_entry.update(
                {
                    "node": node_name,
                    "cycles": cycles,
                    "late_cycles": late,
                    "late_ratio": late_ratio,
                    "silence_ratio": silence_ratio,
                    "avg_abs": avg_abs,
                    "peak_abs": peak_abs,
                    "gap_ms": gap_ms,
                    "expected_ms": expected_ms,
                    "updated_at_unix": now_unix,
                }
            )
            channels[channel] = channel_entry
            entry["channels"] = channels
            entry["updated_at_unix"] = now_unix

        warn_key = f"{mac}:{channel}:late"
        late_warn = late_ratio >= DSP_WARN_LATE_RATIO or gap_ms >= DSP_WARN_GAP_MS
        previous_warn = self._dsp_warning_state.get(warn_key, False)
        self._dsp_warning_state[warn_key] = late_warn
        if late_warn and not previous_warn:
            log.warning(
                "[DSP] speaker=%s channel=%s late_ratio=%.3f gap_ms=%.3f expected_ms=%.3f cycles=%d late=%d",
                format_device_label(mac),
                channel,
                late_ratio,
                gap_ms,
                expected_ms,
                cycles,
                late,
            )
        elif previous_warn and not late_warn:
            log.info(
                "[DSP] speaker=%s channel=%s recovered late_ratio=%.3f gap_ms=%.3f expected_ms=%.3f",
                format_device_label(mac),
                channel,
                late_ratio,
                gap_ms,
                expected_ms,
            )

        heartbeat_key = f"{mac}:{channel}"
        last_hb = self._last_dsp_heartbeat_ts.get(heartbeat_key, 0.0)
        if DSP_LOG_HEARTBEAT_SEC > 0 and (now_unix - last_hb) >= DSP_LOG_HEARTBEAT_SEC:
            self._last_dsp_heartbeat_ts[heartbeat_key] = now_unix
            log.info(
                "[DSP] speaker=%s channel=%s cycles=%d late_ratio=%.3f silence_ratio=%.3f avg_abs=%.6f peak_abs=%.6f gap_ms=%.3f expected_ms=%.3f",
                format_device_label(mac),
                channel,
                cycles,
                late_ratio,
                silence_ratio,
                avg_abs,
                peak_abs,
                gap_ms,
                expected_ms,
            )

        self._persist_dsp_state()

    def _persist_dsp_state(self) -> None:
        try:
            os.makedirs(DSP_STATE_DIR, exist_ok=True)
            with self._lock:
                payload = {
                    "schema": 1,
                    "updated_at_unix": round(time.time(), 3),
                    "outputs": self._dsp_metrics,
                }
            tmp_path = f"{DSP_CALLBACK_STATE_PATH}.tmp"
            with open(tmp_path, "w", encoding="ascii") as fh:
                json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
            os.replace(tmp_path, DSP_CALLBACK_STATE_PATH)
        except Exception as exc:
            log.debug("Failed to persist DSP callback telemetry: %s", exc)

    def _parse_kv_line(self, line: str, prefix: str) -> Dict[str, str]:
        body = line[len(prefix) :].strip()
        values: Dict[str, str] = {}
        for token in body.split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            values[key.strip()] = value.strip()
        return values

    def _mac_from_node_name(self, node_name: str) -> str:
        match = re.match(r"^syncsonic-delay-([0-9a-f_]+)-(fl|fr)$", node_name)
        if not match:
            return ""
        token = match.group(1).upper()
        return token.replace("_", ":")

    def _to_float(self, value: Optional[str]) -> float:
        try:
            return float(value) if value is not None else 0.0
        except Exception:
            return 0.0

    def _to_int(self, value: Optional[str]) -> int:
        try:
            return int(float(value)) if value is not None else 0
        except Exception:
            return 0


_PIPEWIRE_TRANSPORT_MANAGER: Optional[PipeWireTransportManager] = None


def get_pipewire_transport_manager() -> PipeWireTransportManager:
    global _PIPEWIRE_TRANSPORT_MANAGER
    if _PIPEWIRE_TRANSPORT_MANAGER is None:
        _PIPEWIRE_TRANSPORT_MANAGER = PipeWireTransportManager()
    return _PIPEWIRE_TRANSPORT_MANAGER
