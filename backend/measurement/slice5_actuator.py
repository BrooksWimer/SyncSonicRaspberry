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


APPLY_THRESHOLD_MS = 30.0  # 2026-05-31 iter 2: raised 15->30 based on empirical 57-min run showing ~50% cycles still correcting at 15ms; 30 is final value for current measurement-precision regime, future work upgrades the measurement protocol
BURST_AMP_LADDER_X1000 = (300, 600, 950)
BURST_MISS_ESCALATION_THRESHOLD = 1  # slice 18: smart adjustment - escalate on every miss, drop on every success
CONFIDENCE_WINDOW_N = 3  # slice 18.2: act on median of last N successful measurements; gates over-correction from single bad measurements. 3 is sufficient — outliers inflate std which raises the sigma floor, so the gate is conservative under noise.
CONFIDENCE_SIGMA_RATIO = 2.0  # |median_offset| must exceed window_std * this ratio to apply correction; prevents acting on noisy stretches
BURST_AMP_X1000 = BURST_AMP_LADDER_X1000[0]
SOCKET_TIMEOUT_SEC = 0.25
RUNTIME_CORRECTIONS_PATH = Path("/run/syncsonic/runtime_corrections.jsonl")

# Item 1: post-correction settling holdoff
POST_CORRECTION_HOLDOFF_CYCLES = 4  # 4 cycles × ~5s cadence = ~20s of settling time

# Item 2: adaptive per-sample input clamp
CLAMP_HISTORY_N = 12          # rolling history window for per-speaker baseline stats
CLAMP_SIGMA_MULTIPLIER = 3.5  # reject measurements > mean ± 3.5σ from rolling history
CLAMP_BOOTSTRAP_N = 6         # accept all measurements until we have this many history samples

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
        socket_writer: Optional[SocketWriter] = None,
        runtime_corrections_path: Path | str | None = RUNTIME_CORRECTIONS_PATH,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.sockets = {mac.upper(): Path(path) for mac, path in sockets.items()}
        self.apply_threshold_ms = abs(float(apply_threshold_ms))
        self.socket_writer = socket_writer or _send_filter_command
        self.runtime_corrections_path = (
            Path(runtime_corrections_path) if runtime_corrections_path is not None else None
        )
        self.logger = logger or logging.getLogger("measurement.slice5_actuator")
        self.baseline_established: dict[str, bool] = {mac: False for mac in self.sockets}
        self.consecutive_missed_bursts: dict[str, int] = {mac: 0 for mac in self.sockets}
        self.burst_amp_indices: dict[str, int] = {mac: 0 for mac in self.sockets}
        # Slice 18.2: per-speaker sliding window of last N successful measurements.
        # Confidence gate uses median(window) vs target as effective offset, and
        # stdev(window) as the noise estimate that must be exceeded before acting.
        self.measurement_window: dict[str, list[float]] = {mac: [] for mac in self.sockets}
        # Item 1: post-correction settling holdoff counters.
        self.holdoff_remaining: dict[str, int] = {mac: 0 for mac in self.sockets}
        # Item 2: per-speaker rolling history for adaptive input clamping.
        self.measurement_history: dict[str, list[float]] = {mac: [] for mac in self.sockets}

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

    def burst_amp_x1000_for(self, mac: str) -> int:
        normalized = mac.upper()
        self.burst_amp_indices.setdefault(normalized, 0)
        index = self.burst_amp_indices[normalized]
        return BURST_AMP_LADDER_X1000[index]

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
            self.consecutive_missed_bursts.setdefault(mac, 0)
            self.burst_amp_indices.setdefault(mac, 0)
            self.holdoff_remaining.setdefault(mac, 0)
            self.measurement_history.setdefault(mac, [])
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
        self.consecutive_missed_bursts.setdefault(mac, 0)
        self.burst_amp_indices.setdefault(mac, 0)
        self.holdoff_remaining.setdefault(mac, 0)
        self.measurement_history.setdefault(mac, [])
        if os.environ.get("MAVERICK_CORRECTION_STOP") == "1":
            self.emergency_stop()
            return ActuationResult(action="emergency_stop", clock_prior_reset=True)
        if missed_burst:
            self._record_missed_burst(mac)
            return ActuationResult(action="missed", clock_prior_reset=False)
        if not _finite(measured_latency_ms) or not _finite(target_total_ms) or not _finite(current_filter_delay):
            return ActuationResult(action="invalid", clock_prior_reset=False)
        # Slice 18 smart-adjustment: on successful detection, drop one rung down the
        # amplitude ladder (no lower than baseline). Symmetric to escalation in
        # _record_missed_burst, so steady-state at the lowest amp that keeps the
        # speaker detectable. Audibility regression fix.
        previous_amp_index = self.burst_amp_indices.get(mac, 0)
        de_escalated = False
        if previous_amp_index > 0:
            self.burst_amp_indices[mac] = previous_amp_index - 1
            de_escalated = True
        self.consecutive_missed_bursts[mac] = 0
        if de_escalated:
            self._log_action(
                mac,
                "amp_de_escalated",
                previous_amp_x1000=BURST_AMP_LADDER_X1000[previous_amp_index],
                burst_amp_x1000=self.burst_amp_x1000_for(mac),
                burst_amp_ladder_x1000=list(BURST_AMP_LADDER_X1000),
            )
        if not self.baseline_established[mac]:
            self.baseline_established[mac] = True
            self._log_action(mac, "baseline", measured_latency_ms=measured_latency_ms)
            return ActuationResult(action="baseline", clock_prior_reset=False)

        # Slice 18.2: confidence-gated actuation. The confidence window is the
        # only large-offset gate: a big consistent initial offset is corrected,
        # while a noisy/disagreeing window is held.
        offset_single = float(measured_latency_ms) - float(target_total_ms)

        # Item 2: adaptive per-sample input clamp.
        # Always append to history (history tracks physical reality, not filtered signal).
        # Reject into confidence window only when outside mean ± CLAMP_SIGMA_MULTIPLIER * std.
        import statistics as _stat_for_clamp
        history = self.measurement_history.setdefault(mac, [])
        history.append(float(measured_latency_ms))
        if len(history) > CLAMP_HISTORY_N:
            history.pop(0)
        if len(history) >= CLAMP_BOOTSTRAP_N and len(history) > 1:
            history_mean = _stat_for_clamp.mean(history)
            history_std = _stat_for_clamp.stdev(history)
            if history_std > 0.0:
                sigma_distance = abs(float(measured_latency_ms) - history_mean) / history_std
                if sigma_distance > CLAMP_SIGMA_MULTIPLIER:
                    self._log_action(
                        mac,
                        "clamped_outlier",
                        measurement_ms=float(measured_latency_ms),
                        history_mean=history_mean,
                        history_std=history_std,
                        sigma_distance=sigma_distance,
                    )
                    return ActuationResult(action="clamped_outlier", clock_prior_reset=False)

        # Item 1: post-correction settling holdoff.
        # Measurement is added to window even during holdoff — window tracks truth.
        # Only actuation trigger is suppressed.
        if self.holdoff_remaining.get(mac, 0) > 0:
            self.holdoff_remaining[mac] -= 1
            # Still add to the window below so it accumulates during settling.
            window = self.measurement_window.setdefault(mac, [])
            window.append(float(measured_latency_ms))
            if len(window) > CONFIDENCE_WINDOW_N:
                window.pop(0)
            self._log_action(
                mac,
                "holdoff",
                holdoff_remaining_before=self.holdoff_remaining[mac] + 1,
                holdoff_remaining_after=self.holdoff_remaining[mac],
                measured_latency_ms=float(measured_latency_ms),
            )
            return ActuationResult(action="holdoff", clock_prior_reset=False)

        # Add to per-speaker sliding window
        window = self.measurement_window.setdefault(mac, [])
        window.append(float(measured_latency_ms))
        if len(window) > CONFIDENCE_WINDOW_N:
            window.pop(0)

        # Building phase: not enough measurements yet to be confident
        if len(window) < CONFIDENCE_WINDOW_N:
            self._log_action(
                mac, "building_window",
                window_n=len(window),
                window_needed=CONFIDENCE_WINDOW_N,
                measured_latency_ms=measured_latency_ms,
                offset_single_ms=offset_single,
            )
            return ActuationResult(action="building_window", clock_prior_reset=False)

        # Confidence-gated decision: median is robust to outliers, std gates noisy periods
        import statistics as _stat_for_window
        window_median = _stat_for_window.median(window)
        window_std = _stat_for_window.stdev(window) if len(window) > 1 else 0.0
        median_offset = window_median - float(target_total_ms)

        # Within-threshold: median is close to target, no action regardless of std
        if abs(median_offset) < self.apply_threshold_ms:
            self._log_action(
                mac, "within_threshold",
                delta_ms=median_offset,
                window_median_ms=window_median,
                window_std_ms=window_std,
            )
            return ActuationResult(action="within_threshold", clock_prior_reset=False)

        # Confidence gate: signal must exceed noise floor (window_std * sigma ratio)
        confidence_floor = window_std * CONFIDENCE_SIGMA_RATIO
        if abs(median_offset) < confidence_floor:
            self._log_action(
                mac, "insufficient_confidence",
                delta_ms=median_offset,
                window_median_ms=window_median,
                window_std_ms=window_std,
                confidence_floor_ms=confidence_floor,
                window_n=len(window),
            )
            return ActuationResult(action="insufficient_confidence", clock_prior_reset=False)

        # Apply correction using median-derived offset (NOT single-measurement offset).
        # Reset window after applying since the speaker has just moved; otherwise pre-move
        # measurements would bias the next decision.
        new_delay = max(0.0, float(current_filter_delay) - median_offset)
        self.measurement_window[mac] = []
        # Item 1: arm the post-correction holdoff so transient settle measurements
        # don't immediately re-trigger another correction.
        self.holdoff_remaining[mac] = POST_CORRECTION_HOLDOFF_CYCLES
        self.set_delay(mac, new_delay)
        offset = median_offset  # for downstream logging compatibility
        self._log_action(
            mac,
            "corrected",
            measured_latency_ms=measured_latency_ms,
            target_total_ms=target_total_ms,
            current_filter_delay_ms=current_filter_delay,
            delta_ms=offset,
            new_filter_delay_ms=new_delay,
            new_delay_ms=new_delay,
        )
        self._write_runtime_correction(
            mac,
            measured_latency_ms=float(measured_latency_ms),
            target_total_ms=float(target_total_ms),
            current_filter_delay_ms=float(current_filter_delay),
            delta_ms=offset,
            new_filter_delay_ms=new_delay,
        )
        return ActuationResult(action="corrected", delta_ms=offset, clock_prior_reset=True)

    def set_delay(self, speaker_id: str, delay_ms: float) -> Optional[dict[str, Any]]:
        return self._write(speaker_id, f"set_delay {delay_ms:.3f}")

    def _record_missed_burst(self, mac: str) -> None:
        self.consecutive_missed_bursts[mac] += 1
        previous_index = self.burst_amp_indices[mac]
        escalated = False
        if (
            self.consecutive_missed_bursts[mac] >= BURST_MISS_ESCALATION_THRESHOLD
            and previous_index < len(BURST_AMP_LADDER_X1000) - 1
        ):
            self.burst_amp_indices[mac] = previous_index + 1
            self.consecutive_missed_bursts[mac] = 0
            escalated = True
        self._log_action(
            mac,
            "missed",
            consecutive_missed_bursts=self.consecutive_missed_bursts[mac],
            burst_amp_x1000=self.burst_amp_x1000_for(mac),
            burst_amp_ladder_x1000=list(BURST_AMP_LADDER_X1000),
            burst_amp_escalated=escalated,
        )

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
        payload = {
            "event": "slice5_actuation",
            "phase": "runtime_correction",
            "timestamp_iso": _timestamp_iso(),
            "mac": mac,
            "action": action,
            **fields,
        }
        self.logger.info(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )
        if self.runtime_corrections_path is None:
            return
        try:
            self.runtime_corrections_path.parent.mkdir(parents=True, exist_ok=True)
            with self.runtime_corrections_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError as exc:
            self.logger.warning(
                "failed to write action event to %s: %s",
                self.runtime_corrections_path,
                exc,
            )

    def _write_runtime_correction(self, mac: str, **fields: Any) -> None:
        if self.runtime_corrections_path is None:
            return
        payload = {
            "event": "runtime_correction",
            "phase": "runtime_correction",
            "timestamp_iso": _timestamp_iso(),
            "mac": mac,
            "action": "corrected",
            **fields,
        }
        try:
            self.runtime_corrections_path.parent.mkdir(parents=True, exist_ok=True)
            with self.runtime_corrections_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError as exc:
            self.logger.warning(
                "failed to write runtime correction event to %s: %s",
                self.runtime_corrections_path,
                exc,
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
