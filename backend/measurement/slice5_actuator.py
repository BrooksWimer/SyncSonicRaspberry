"""Slice 5 delay-step actuator for runtime ultrasonic corrections."""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


APPLY_THRESHOLD_MS = 1.0
FREAK_THRESHOLD_MS = 100.0
BURST_AMP_X1000 = 300
SOCKET_TIMEOUT_SEC = 0.25

BleStopCallback = Callable[[], None]
SocketWriter = Callable[[Path, str], Optional[dict[str, Any]]]

_BLE_STOP_CALLBACKS: list[BleStopCallback] = []


@dataclass(frozen=True)
class ActuationResult:
    action: str
    delta_ms: float = 0.0
    clock_prior_reset: bool = False


def register_ble_stop_callback(fn: BleStopCallback) -> None:
    """Register a BLE stop hook that can be called by the runtime layer."""
    _BLE_STOP_CALLBACKS.append(fn)


class SpeakerActuator:
    """Apply startup-tune style delay corrections to each valid burst."""

    def __init__(
        self,
        sockets: Mapping[str, Path | str],
        *,
        apply_threshold_ms: float = APPLY_THRESHOLD_MS,
        freak_threshold_ms: float = FREAK_THRESHOLD_MS,
        socket_writer: Optional[SocketWriter] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.sockets = {mac.upper(): Path(path) for mac, path in sockets.items()}
        self.apply_threshold_ms = abs(float(apply_threshold_ms))
        self.freak_threshold_ms = abs(float(freak_threshold_ms))
        self.socket_writer = socket_writer or _send_filter_command
        self.logger = logger or logging.getLogger("measurement.slice5_actuator")
        self.baseline_established: dict[str, bool] = {mac: False for mac in self.sockets}

    @property
    def states(self) -> dict[str, str]:
        return {
            mac: ("BASELINE_ESTABLISHED" if established else "AWAITING_BASELINE")
            for mac, established in self.baseline_established.items()
        }

    def state_for(self, mac: str) -> str:
        return self.states.get(mac.upper(), "AWAITING_BASELINE")

    def baseline_for(self, mac: str) -> Optional[float]:
        return 0.0 if self.baseline_established.get(mac.upper(), False) else None

    def register_signal_handler(self, signum: int = signal.SIGUSR1) -> None:
        signal.signal(signum, lambda _signum, _frame: self.emergency_stop())

    def sync_sockets(self, sockets: Mapping[str, Path | str]) -> None:
        new_sockets = {mac.upper(): Path(path) for mac, path in sockets.items()}
        previous = set(self.sockets)
        current = set(new_sockets)
        for mac in sorted(previous - current):
            self.baseline_established[mac] = False
            self._log_baseline_reset(mac, "speaker_disconnected")
        for mac in sorted(current):
            self.baseline_established.setdefault(mac, False)
        self.sockets = new_sockets

    def re_enable(self, mac: Optional[str] = None) -> None:
        macs = [mac.upper()] if mac else sorted(self.baseline_established)
        for item in macs:
            self.baseline_established[item] = False
            self._log_baseline_reset(item, "operator_reenable")

    def apply(
        self,
        speaker_id: str,
        measured_latency_ms: Optional[float],
        target_total_ms: Optional[float],
        current_filter_delay: Optional[float],
        *,
        missed_burst: bool = False,
    ) -> ActuationResult:
        mac = speaker_id.upper()
        self.baseline_established.setdefault(mac, False)
        if os.environ.get("MAVERICK_CORRECTION_STOP") == "1":
            self.emergency_stop()
            return ActuationResult(action="emergency_stop", clock_prior_reset=True)
        if missed_burst:
            return ActuationResult(action="missed", clock_prior_reset=False)
        if not _finite(measured_latency_ms) or not _finite(target_total_ms) or not _finite(current_filter_delay):
            return ActuationResult(action="invalid", clock_prior_reset=False)
        if not self.baseline_established[mac]:
            self.baseline_established[mac] = True
            self._log_action(mac, "baseline", measured_latency_ms=measured_latency_ms)
            return ActuationResult(action="baseline", clock_prior_reset=False)

        offset = float(measured_latency_ms) - float(target_total_ms)
        if abs(offset) < self.apply_threshold_ms:
            self._log_action(mac, "within_threshold", delta_ms=offset)
            return ActuationResult(action="within_threshold", clock_prior_reset=False)
        if abs(offset) >= self.freak_threshold_ms:
            self.logger.warning(
                json.dumps(
                    {
                        "event": "slice5_freak_outlier_skip",
                        "timestamp_iso": _timestamp_iso(),
                        "mac": mac,
                        "measured_latency_ms": measured_latency_ms,
                        "target_total_ms": target_total_ms,
                        "offset_ms": offset,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return ActuationResult(action="freak_skip", clock_prior_reset=False)

        new_delay = float(current_filter_delay) + offset
        self.set_delay(mac, new_delay)
        self._log_action(mac, "corrected", delta_ms=offset, new_delay_ms=new_delay)
        return ActuationResult(action="corrected", delta_ms=offset, clock_prior_reset=True)

    def set_delay(self, speaker_id: str, delay_ms: float) -> Optional[dict[str, Any]]:
        return self._write(speaker_id, f"set_delay {delay_ms:.3f}")

    def emergency_stop(self) -> None:
        started = time.monotonic()
        for mac in sorted(self.sockets):
            self._write(mac, "set_rate_ppm 0")
            self.baseline_established[mac] = False
            self._log_baseline_reset(mac, "EMERGENCY_STOP")
        elapsed = time.monotonic() - started
        self.logger.info(
            json.dumps(
                {
                    "event": "slice5_emergency_stop",
                    "timestamp_iso": _timestamp_iso(),
                    "speaker_count": len(self.sockets),
                    "elapsed_sec": elapsed,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def _write(self, mac: str, command: str) -> Optional[dict[str, Any]]:
        path = self.sockets.get(mac.upper())
        if path is None:
            self._log_action(mac.upper(), "socket_not_found")
            return None
        return self.socket_writer(path, command)

    def _log_baseline_reset(self, mac: str, reason: str) -> None:
        self.logger.info(
            json.dumps(
                {
                    "event": "slice5_baseline_reset",
                    "timestamp_iso": _timestamp_iso(),
                    "mac": mac,
                    "reason": reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def _log_action(self, mac: str, action: str, **fields: Any) -> None:
        self.logger.info(
            json.dumps(
                {
                    "event": "slice5_actuation",
                    "timestamp_iso": _timestamp_iso(),
                    "mac": mac,
                    "action": action,
                    **fields,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )


def trigger_ble_stop_callbacks() -> None:
    for callback in list(_BLE_STOP_CALLBACKS):
        callback()


def _send_filter_command(socket_path: Path, payload: str) -> Optional[dict[str, Any]]:
    if not socket_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(SOCKET_TIMEOUT_SEC)
            sock.connect(str(socket_path))
            sock.sendall((payload + "\n").encode("ascii"))
            buf = b""
            while b"\n" not in buf and len(buf) < 4096:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout):
        return None
    try:
        line = buf.decode("ascii", errors="replace").strip().split("\n")[0]
        return json.loads(line)
    except (IndexError, json.JSONDecodeError, ValueError):
        return None


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _timestamp_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
