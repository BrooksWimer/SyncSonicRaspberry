"""Slice 5 safety-gated actuator for runtime ultrasonic corrections."""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import socket
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Mapping, Optional


SLIDER_THRESHOLD_MS = 5.0
WARMUP_CYCLES_REQUIRED = 3
MISS_RATE_WINDOW = 10
MISS_RATE_SUSPEND_THRESHOLD = 0.30
MAX_RATE_PPM = 50
BURST_AMP_X1000 = 300
SAMPLE_RATE = 48_000
SOCKET_TIMEOUT_SEC = 0.25

WARMING_UP = "WARMING_UP"
ACTIVE = "ACTIVE"
SUSPENDED = "SUSPENDED"

BleStopCallback = Callable[[], None]
SocketWriter = Callable[[Path, str], Optional[dict[str, Any]]]

_BLE_STOP_CALLBACKS: list[BleStopCallback] = []


@dataclass(frozen=True)
class ActuationResult:
    mac: str
    state: str
    actuation_applied_ppm: float
    skip_reason: Optional[str]
    slider_applied_ms: float


@dataclass
class _SpeakerState:
    state: str = WARMING_UP
    clean_warmup_cycles: int = 0
    misses: Deque[bool] | None = None


def register_ble_stop_callback(fn: BleStopCallback) -> None:
    """Register a BLE stop hook that can be called by the runtime layer."""
    _BLE_STOP_CALLBACKS.append(fn)


class SpeakerActuator:
    """Apply relative drift proposals through a guarded two-stage controller."""

    def __init__(
        self,
        sockets: Mapping[str, Path | str],
        *,
        slider_threshold_ms: float = SLIDER_THRESHOLD_MS,
        warmup_cycles_required: int = WARMUP_CYCLES_REQUIRED,
        miss_rate_window: int = MISS_RATE_WINDOW,
        miss_rate_suspend_threshold: float = MISS_RATE_SUSPEND_THRESHOLD,
        max_rate_ppm: float = MAX_RATE_PPM,
        sample_rate: int = SAMPLE_RATE,
        socket_writer: Optional[SocketWriter] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.sockets = {mac.upper(): Path(path) for mac, path in sockets.items()}
        self.slider_threshold_ms = float(slider_threshold_ms)
        self.warmup_cycles_required = max(1, int(warmup_cycles_required))
        self.miss_rate_window = max(1, int(miss_rate_window))
        self.miss_rate_suspend_threshold = float(miss_rate_suspend_threshold)
        self.max_rate_ppm = abs(float(max_rate_ppm))
        self.sample_rate = int(sample_rate)
        self.socket_writer = socket_writer or _send_filter_command
        self.logger = logger or logging.getLogger("measurement.slice5_actuator")
        self._states: dict[str, _SpeakerState] = {
            mac: _SpeakerState(misses=deque(maxlen=self.miss_rate_window)) for mac in self.sockets
        }

    @property
    def states(self) -> dict[str, str]:
        return {mac: state.state for mac, state in self._states.items()}

    def state_for(self, mac: str) -> str:
        return self._state(mac).state

    def register_signal_handler(self, signum: int = signal.SIGUSR1) -> None:
        signal.signal(signum, lambda _signum, _frame: self.emergency_stop())

    def re_enable(self, mac: Optional[str] = None) -> None:
        macs = [mac.upper()] if mac else sorted(self._states)
        for item in macs:
            state = self._state(item)
            old = state.state
            state.state = WARMING_UP
            state.clean_warmup_cycles = 0
            state.misses = deque(maxlen=self.miss_rate_window)
            self._log_transition(item, old, state.state, "operator_reenable")

    def apply(self, proposal: Any) -> ActuationResult:
        if os.environ.get("MAVERICK_CORRECTION_STOP") == "1":
            self.emergency_stop()
            mac = str(_proposal_get(proposal, "mac", "")).upper()
            return self._result(mac, SUSPENDED, "EMERGENCY_STOP")

        mac = str(_proposal_get(proposal, "mac", "")).upper()
        if not mac:
            raise ValueError("proposal is missing mac")
        state = self._state(mac)

        missed = _proposal_bool(proposal, "missed_burst", default=False)
        self._record_miss(mac, state, missed)
        if state.state == SUSPENDED:
            return self._result(mac, state.state, "SUSPENDED")

        if missed:
            if state.state == WARMING_UP:
                state.clean_warmup_cycles = 0
            return self._result(mac, state.state, "SKIP_MISSED")

        if self._confidence_drop(proposal):
            self._suspend(mac, state, "CONFIDENCE_DROP")
            return self._result(mac, state.state, "CONFIDENCE_DROP")

        proposed_ppm = _proposal_float(
            proposal,
            "proposed_adjustment_ppm",
            fallback_keys=("proposed_rate_ppm", "applied_ppm"),
        )
        max_ppm = abs(_proposal_float(proposal, "max_ppm", default=self.max_rate_ppm))
        clamped = _is_clamped(proposed_ppm, max_ppm)
        if clamped:
            self._log_skip(mac, "SKIP_CLAMPED", proposed_ppm=proposed_ppm, max_ppm=max_ppm)
            return ActuationResult(mac, state.state, 0.0, "SKIP_CLAMPED", 0.0)

        proposal_warming = _proposal_get(proposal, "reason") == "warming_up" or _proposal_bool(
            proposal,
            "warming",
            default=False,
        )
        if state.state == WARMING_UP:
            if not missed and not proposal_warming and not clamped and math.isfinite(proposed_ppm):
                state.clean_warmup_cycles += 1
                if state.clean_warmup_cycles >= self.warmup_cycles_required:
                    self._log_transition(mac, state.state, ACTIVE, "warmup_clean_cycles")
                    state.state = ACTIVE
            else:
                state.clean_warmup_cycles = 0
            return self._result(mac, state.state, "WARMING_UP")

        if state.state != ACTIVE:
            return self._result(mac, state.state, state.state)

        residual_ms = _proposal_float(
            proposal,
            "residual_ms",
            fallback_keys=("relative_residual_ms", "recent_relative_residual_ms"),
            default=0.0,
        )
        current_filter_delay_ms = _proposal_float(
            proposal,
            "current_filter_delay_ms",
            fallback_keys=("slider_target_delay_ms",),
            default=0.0,
        )
        if abs(residual_ms) > self.slider_threshold_ms:
            target_delay_ms = max(0.0, current_filter_delay_ms + residual_ms)
            target_delay_samples = int(round(target_delay_ms * self.sample_rate / 1000.0))
            response = self._write(mac, f"set_delay {target_delay_ms:.3f}")
            self.logger.info(
                json.dumps(
                    {
                        "event": "slice5_slider_adjustment",
                        "timestamp_iso": _timestamp_iso(),
                        "mac": mac,
                        "residual_ms": residual_ms,
                        "current_filter_delay_ms": current_filter_delay_ms,
                        "target_delay_ms": target_delay_ms,
                        "target_delay_samples": target_delay_samples,
                        "response": response,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return ActuationResult(mac, state.state, 0.0, None, target_delay_ms - current_filter_delay_ms)

        applied_ppm = max(-self.max_rate_ppm, min(self.max_rate_ppm, proposed_ppm))
        response = self._write(mac, f"set_rate_ppm {applied_ppm:.3f}")
        self.logger.info(
            json.dumps(
                {
                    "event": "slice5_rate_adjustment",
                    "timestamp_iso": _timestamp_iso(),
                    "mac": mac,
                    "actuation_applied_ppm": applied_ppm,
                    "proposed_adjustment_ppm": proposed_ppm,
                    "response": response,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return ActuationResult(mac, state.state, applied_ppm, None, 0.0)

    def emergency_stop(self) -> None:
        started = time.monotonic()
        for mac in sorted(self.sockets):
            self._write(mac, "set_rate_ppm 0")
            state = self._state(mac)
            old = state.state
            state.state = SUSPENDED
            state.clean_warmup_cycles = 0
            self._log_transition(mac, old, SUSPENDED, "EMERGENCY_STOP")
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

    def _state(self, mac: str) -> _SpeakerState:
        mac = mac.upper()
        if mac not in self._states:
            self._states[mac] = _SpeakerState(misses=deque(maxlen=self.miss_rate_window))
        return self._states[mac]

    def _record_miss(self, mac: str, state: _SpeakerState, missed: bool) -> None:
        if state.misses is None:
            state.misses = deque(maxlen=self.miss_rate_window)
        state.misses.append(bool(missed))
        if len(state.misses) < self.miss_rate_window:
            return
        miss_rate = sum(1 for item in state.misses if item) / len(state.misses)
        if miss_rate > self.miss_rate_suspend_threshold:
            self._suspend(mac, state, f"MISS_RATE_{miss_rate:.3f}")

    def _confidence_drop(self, proposal: Any) -> bool:
        if _proposal_bool(proposal, "confidence_drop", default=False):
            return True
        event = _proposal_get(proposal, "event")
        reason = _proposal_get(proposal, "reason")
        if event == "relative_correction_skipped" and reason in {"low_snr", "confidence_drop"}:
            return True
        confidence = _proposal_float(proposal, "confidence", default=1.0)
        return math.isfinite(confidence) and confidence < 0.0

    def _suspend(self, mac: str, state: _SpeakerState, reason: str) -> None:
        old = state.state
        state.state = SUSPENDED
        state.clean_warmup_cycles = 0
        self._log_transition(mac, old, SUSPENDED, reason)

    def _write(self, mac: str, command: str) -> Optional[dict[str, Any]]:
        path = self.sockets.get(mac.upper())
        if path is None:
            self.logger.info(
                json.dumps(
                    {
                        "event": "slice5_skip",
                        "timestamp_iso": _timestamp_iso(),
                        "mac": mac,
                        "reason": "SOCKET_NOT_FOUND",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return None
        return self.socket_writer(path, command)

    def _result(self, mac: str, state: str, skip_reason: Optional[str]) -> ActuationResult:
        if mac:
            self._log_skip(mac, skip_reason or state)
        return ActuationResult(mac, state, 0.0, skip_reason, 0.0)

    def _log_transition(self, mac: str, old: str, new: str, reason: str) -> None:
        if old == new and reason != "EMERGENCY_STOP":
            return
        self.logger.info(
            json.dumps(
                {
                    "event": "slice5_state_transition",
                    "timestamp_iso": _timestamp_iso(),
                    "mac": mac,
                    "old_state": old,
                    "new_state": new,
                    "reason": reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def _log_skip(self, mac: str, reason: str, **fields: Any) -> None:
        self.logger.info(
            json.dumps(
                {
                    "event": "slice5_actuation_skipped",
                    "timestamp_iso": _timestamp_iso(),
                    "mac": mac,
                    "state": self.state_for(mac),
                    "reason": reason,
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


def _proposal_get(proposal: Any, key: str, default: Any = None) -> Any:
    if isinstance(proposal, Mapping):
        return proposal.get(key, default)
    return getattr(proposal, key, default)


def _proposal_float(
    proposal: Any,
    key: str,
    *,
    fallback_keys: tuple[str, ...] = (),
    default: float = math.nan,
) -> float:
    value = _proposal_get(proposal, key, None)
    if value is None:
        for fallback in fallback_keys:
            value = _proposal_get(proposal, fallback, None)
            if value is not None:
                break
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _proposal_bool(proposal: Any, key: str, *, default: bool) -> bool:
    value = _proposal_get(proposal, key, default)
    return bool(value)


def _is_clamped(value: float, max_ppm: float) -> bool:
    return math.isfinite(value) and math.isclose(abs(value), abs(max_ppm), rel_tol=0.0, abs_tol=1e-9)


def _timestamp_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
