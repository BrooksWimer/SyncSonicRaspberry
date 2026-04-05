from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import Lock
from typing import Dict, Optional

from syncsonic_ble.helpers.pulseaudio_helpers import create_loopback, remove_loopback_for_device
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)


@dataclass
class OutputActuationState:
    mac: str
    delay_cmd_ms: float = 100.0
    resample_ratio_ppm: float = 0.0
    drift_ppm: float = 0.0
    raw_offset_ms: float = 0.0
    filtered_offset_ms: float = 0.0
    correction_events: int = 0
    relock_events: int = 0
    xrun_count: int = 0
    active: bool = False
    mode: str = "idle"
    backend: str = "pulseaudio-loopback"


class ActuationManager:
    """Owns commanded per-speaker timing state instead of treating pactl as the source of truth."""

    FILTER_ALPHA = 0.35
    OFFSET_TOLERANCE_MS = 2.0
    RESYNC_THRESHOLD_MS = 50.0
    MAX_SLEW_STEP_MS = 5.0
    MIN_DELAY_MS = 20.0
    MAX_DELAY_MS = 4000.0
    MAX_RATE_PPM = 300.0
    DRIFT_PPM_PER_MS = 20.0

    def __init__(self) -> None:
        self._lock = Lock()
        self._states: Dict[str, OutputActuationState] = {}

    def ensure_output(self, mac: str, initial_delay_ms: float = 100.0) -> OutputActuationState:
        mac = mac.upper()
        with self._lock:
            state = self._states.get(mac)
            if state is None:
                state = OutputActuationState(
                    mac=mac,
                    delay_cmd_ms=self._clamp_delay(initial_delay_ms),
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
        remove_loopback_for_device(mac)
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
        mac = mac.upper()
        state = self.ensure_output(mac, initial_delay_ms=delay_ms)
        with self._lock:
            state.mode = "manual"
            state.raw_offset_ms = 0.0
            state.filtered_offset_ms = 0.0
        ok = self._apply_delay(mac, delay_ms)
        return ok, self.get_status_snapshot(mac)

    def apply_relative_measurement(self, reference_mac: str, target_mac: str, offset_ms: float):
        """
        Apply a two-speaker measurement.

        offset_ms > 0 means target is late relative to reference.
        We only add delay, so the currently early speaker gets delayed.
        """
        reference_mac = reference_mac.upper()
        target_mac = target_mac.upper()
        ref_state = self.ensure_output(reference_mac)
        target_state = self.ensure_output(target_mac)

        with self._lock:
            ref_state.raw_offset_ms = 0.0
            ref_state.filtered_offset_ms = 0.0
            target_state.raw_offset_ms = offset_ms
            target_state.filtered_offset_ms = (
                self.FILTER_ALPHA * offset_ms
                + (1.0 - self.FILTER_ALPHA) * target_state.filtered_offset_ms
            )
            filtered = target_state.filtered_offset_ms
            target_state.drift_ppm = self._clamp_rate_ppm(filtered * self.DRIFT_PPM_PER_MS)
            target_state.resample_ratio_ppm = target_state.drift_ppm

        if abs(filtered) <= self.OFFSET_TOLERANCE_MS:
            with self._lock:
                ref_state.mode = "lock"
                target_state.mode = "lock"
            return {
                "applied": False,
                "action": "none",
                "offset_ms": offset_ms,
                "filtered_offset_ms": filtered,
                "target_mac": target_mac,
                "reference_mac": reference_mac,
                "states": self.get_status_snapshot(),
            }

        delayed_mac = reference_mac if filtered > 0 else target_mac
        delayed_state = self.ensure_output(delayed_mac)
        magnitude_ms = abs(filtered)
        relock = magnitude_ms >= self.RESYNC_THRESHOLD_MS
        step_ms = magnitude_ms if relock else min(magnitude_ms, self.MAX_SLEW_STEP_MS)

        with self._lock:
            delayed_state.correction_events += 1
            delayed_state.mode = "relock" if relock else "slew"
            if relock:
                delayed_state.relock_events += 1
            target_delay = delayed_state.delay_cmd_ms + step_ms

        ok = self._apply_delay(delayed_mac, target_delay)
        with self._lock:
            delayed_state.mode = "lock" if ok else "error"

        return {
            "applied": ok,
            "action": "relock" if relock else "slew",
            "offset_ms": offset_ms,
            "filtered_offset_ms": filtered,
            "adjusted_mac": delayed_mac,
            "target_delay_ms": self.get_commanded_delay(delayed_mac),
            "reference_mac": reference_mac,
            "target_mac": target_mac,
            "states": self.get_status_snapshot(),
        }

    def _apply_delay(self, mac: str, delay_ms: float) -> bool:
        target_delay = self._clamp_delay(delay_ms)
        sink_prefix = f"bluez_sink.{mac.replace(':', '_')}"
        ok = create_loopback(sink_prefix, latency_ms=int(round(target_delay)))
        if ok:
            with self._lock:
                state = self._states[mac]
                state.delay_cmd_ms = target_delay
                state.active = True
        else:
            log.warning("Actuation delay apply failed for %s -> %.2f ms", mac, target_delay)
        return ok

    def _clamp_delay(self, delay_ms: float) -> float:
        return max(self.MIN_DELAY_MS, min(self.MAX_DELAY_MS, float(delay_ms)))

    def _clamp_rate_ppm(self, ppm: float) -> float:
        return max(-self.MAX_RATE_PPM, min(self.MAX_RATE_PPM, float(ppm)))


_ACTUATION_MANAGER: Optional[ActuationManager] = None


def get_actuation_manager() -> ActuationManager:
    global _ACTUATION_MANAGER
    if _ACTUATION_MANAGER is None:
        _ACTUATION_MANAGER = ActuationManager()
    return _ACTUATION_MANAGER
