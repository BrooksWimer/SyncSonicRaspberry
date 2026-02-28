from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import Lock
from typing import Dict, Optional

from syncsonic_ble.helpers.actuation_backends import get_actuation_backend
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)


@dataclass
class OutputActuationState:
    mac: str
    delay_cmd_ms: float = 100.0
    delay_applied_ms: float = 100.0
    delay_line_applied_ms: float = 0.0
    resample_ratio_ppm: float = 0.0
    applied_ratio_ppm: float = 0.0
    drift_ppm: float = 0.0
    raw_offset_ms: float = 0.0
    filtered_offset_ms: float = 0.0
    correction_events: int = 0
    relock_events: int = 0
    xrun_count: int = 0
    active: bool = False
    mode: str = "idle"
    backend: str = "pulseaudio-loopback"
    control_path: str = ""
    backend_reason: str = ""
    shadow_fallback: bool = False


class ActuationManager:
    """Tracks commanded per-speaker timing state and fallback loopback actuation."""

    MIN_DELAY_MS = 20.0
    MAX_DELAY_MS = 4000.0

    def __init__(self) -> None:
        self._lock = Lock()
        self._states: Dict[str, OutputActuationState] = {}
        self._backend = get_actuation_backend()

    def ensure_output(self, mac: str, initial_delay_ms: float = 100.0) -> OutputActuationState:
        mac = mac.upper()
        with self._lock:
            state = self._states.get(mac)
            if state is None:
                state = OutputActuationState(
                    mac=mac,
                    delay_cmd_ms=self._clamp_delay(initial_delay_ms),
                    backend=self._backend.name,
                )
                self._states[mac] = state
            return state

    def set_output_active(self, mac: str, active: bool) -> None:
        state = self.ensure_output(mac)
        with self._lock:
            state.active = active
            state.mode = "lock" if active else "idle"

    def remove_output(self, mac: str) -> None:
        mac = mac.upper()
        self._backend.remove_output(mac)
        with self._lock:
            state = self._states.get(mac)
            if state is not None:
                state.active = False
                state.mode = "idle"

    def get_commanded_delay(self, mac: str, default_ms: float = 100.0) -> float:
        state = self.ensure_output(mac, initial_delay_ms=default_ms)
        with self._lock:
            return state.delay_cmd_ms

    def get_status_snapshot(self, mac: Optional[str] = None):
        with self._lock:
            if mac is not None:
                state = self._states.get(mac.upper())
                return asdict(state) if state else None
            return {device_mac: asdict(state) for device_mac, state in self._states.items()}

    def set_manual_delay(self, mac: str, delay_ms: float):
        ok = self.record_applied_delay(mac, delay_ms, mode="manual", apply_loopback=True)
        return ok, self.get_status_snapshot(mac)

    def record_applied_delay(
        self,
        mac: str,
        delay_ms: float,
        *,
        mode: str,
        apply_loopback: bool = False,
    ) -> bool:
        mac = mac.upper()
        self.ensure_output(mac, initial_delay_ms=delay_ms)
        with self._lock:
            state = self._states[mac]
            state.mode = mode
            state.raw_offset_ms = 0.0
            state.filtered_offset_ms = 0.0
            state.drift_ppm = 0.0
            state.resample_ratio_ppm = 0.0
            state.applied_ratio_ppm = 0.0
        if apply_loopback:
            return self._apply_control(mac, delay_ms, 0.0, mode=mode)
        target_delay = self._clamp_delay(delay_ms)
        with self._lock:
            state = self._states[mac]
            state.delay_cmd_ms = target_delay
            state.delay_applied_ms = target_delay
            state.delay_line_applied_ms = 0.0
            state.active = True
            state.mode = mode
        return True

    def record_measurement(
        self,
        reference_mac: str,
        target_mac: str,
        *,
        raw_offset_ms: float,
        filtered_offset_ms: float,
        ratio_ppm: float = 0.0,
        mode: str = "measure",
    ) -> None:
        reference_mac = reference_mac.upper()
        target_mac = target_mac.upper()
        self.ensure_output(reference_mac)
        self.ensure_output(target_mac)
        with self._lock:
            ref_state = self._states[reference_mac]
            target_state = self._states[target_mac]
            ref_state.raw_offset_ms = 0.0
            ref_state.filtered_offset_ms = 0.0
            ref_state.drift_ppm = 0.0
            ref_state.resample_ratio_ppm = 0.0
            ref_state.mode = "reference"
            target_state.raw_offset_ms = float(raw_offset_ms)
            target_state.filtered_offset_ms = float(filtered_offset_ms)
            target_state.drift_ppm = float(ratio_ppm)
            target_state.resample_ratio_ppm = float(ratio_ppm)
            target_state.mode = mode

    def set_mode(self, mac: str, mode: str) -> None:
        state = self.ensure_output(mac)
        with self._lock:
            state.mode = mode

    def note_auto_sync_action(self, mac: str, *, relock: bool) -> None:
        state = self.ensure_output(mac)
        with self._lock:
            state.correction_events += 1
            if relock:
                state.relock_events += 1
            state.mode = "relock" if relock else "slew"

    def record_rate_target(self, mac: str, ratio_ppm: float, *, mode: str) -> None:
        state = self.ensure_output(mac)
        with self._lock:
            state.drift_ppm = float(ratio_ppm)
            state.resample_ratio_ppm = float(ratio_ppm)
            state.mode = mode

    def apply_fallback_delay(self, mac: str, delay_ms: float, *, mode: str):
        current_rate_ppm = 0.0
        state = self.ensure_output(mac, initial_delay_ms=delay_ms)
        with self._lock:
            current_rate_ppm = state.resample_ratio_ppm
        ok = self._apply_control(mac, delay_ms, current_rate_ppm, mode=mode)
        return ok, self.get_status_snapshot(mac)

    def apply_control_target(self, mac: str, *, delay_ms: float, rate_ppm: float, mode: str):
        ok = self._apply_control(mac, delay_ms, rate_ppm, mode=mode)
        return ok, self.get_status_snapshot(mac)

    def _apply_control(self, mac: str, delay_ms: float, rate_ppm: float, *, mode: str) -> bool:
        mac = mac.upper()
        self.ensure_output(mac, initial_delay_ms=delay_ms)
        target_delay = self._clamp_delay(delay_ms)
        target_rate_ppm = float(rate_ppm)
        result = self._backend.apply_control(mac, target_delay, target_rate_ppm, mode=mode)
        if result.ok:
            with self._lock:
                state = self._states[mac]
                state.delay_cmd_ms = float(result.applied_delay_ms or target_delay)
                state.delay_applied_ms = float(result.applied_delay_ms or target_delay)
                state.delay_line_applied_ms = max(0.0, state.delay_applied_ms - 120.0)
                state.active = True
                state.mode = mode
                state.backend = result.backend
                state.resample_ratio_ppm = float(result.applied_rate_ppm)
                state.drift_ppm = float(result.applied_rate_ppm)
                state.applied_ratio_ppm = float(result.applied_rate_ppm)
                state.control_path = result.control_path
                state.backend_reason = result.reason
                state.shadow_fallback = result.shadow_fallback
        else:
            log.warning(
                "Actuation control apply failed for %s -> %.2f ms / %.2f ppm via %s (%s)",
                mac,
                target_delay,
                target_rate_ppm,
                result.backend,
                result.reason,
            )
            with self._lock:
                state = self._states[mac]
                state.mode = "error"
                state.backend = result.backend
                state.control_path = result.control_path
                state.backend_reason = result.reason
                state.shadow_fallback = result.shadow_fallback
        return result.ok

    def _clamp_delay(self, delay_ms: float) -> float:
        return max(self.MIN_DELAY_MS, min(self.MAX_DELAY_MS, float(delay_ms)))


_ACTUATION_MANAGER: Optional[ActuationManager] = None


def get_actuation_manager() -> ActuationManager:
    global _ACTUATION_MANAGER
    if _ACTUATION_MANAGER is None:
        _ACTUATION_MANAGER = ActuationManager()
    return _ACTUATION_MANAGER
