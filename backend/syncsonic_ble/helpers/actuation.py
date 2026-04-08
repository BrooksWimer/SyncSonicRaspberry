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
    active: bool = False
    mode: str = "idle"
    backend: str = "pulseaudio-loopback"
    control_path: str = ""
    backend_reason: str = ""


class ActuationManager:
    """Tracks manual per-speaker timing state for the neutral foundation."""

    MIN_DELAY_MS = 20.0
    MAX_DELAY_MS = 4000.0
    TRANSPORT_BASE_MS = 120.0

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
            state.mode = "manual" if active else "idle"

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
        return self.apply_control_target(mac, delay_ms=delay_ms, rate_ppm=0.0, mode="manual")

    def apply_fallback_delay(self, mac: str, delay_ms: float, *, mode: str):
        ok = self._apply_control(mac, delay_ms, mode=mode)
        return ok, self.get_status_snapshot(mac)

    def apply_control_target(self, mac: str, *, delay_ms: float, rate_ppm: float, mode: str):
        ok = self._apply_control(mac, delay_ms, mode=mode)
        return ok, self.get_status_snapshot(mac)

    def _apply_control(self, mac: str, delay_ms: float, *, mode: str) -> bool:
        mac = mac.upper()
        self.ensure_output(mac, initial_delay_ms=delay_ms)
        target_delay = self._clamp_delay(delay_ms)
        result = self._backend.apply_control(mac, target_delay, 0.0, mode=mode)
        if result.ok:
            with self._lock:
                state = self._states[mac]
                state.delay_cmd_ms = float(result.applied_delay_ms or target_delay)
                state.delay_applied_ms = float(result.applied_delay_ms or target_delay)
                state.delay_line_applied_ms = max(0.0, state.delay_applied_ms - self.TRANSPORT_BASE_MS)
                state.active = True
                state.mode = mode
                state.backend = result.backend
                state.control_path = result.control_path
                state.backend_reason = result.reason
        else:
            log.warning(
                "Actuation control apply failed for %s -> %.2f ms via %s (%s)",
                mac,
                target_delay,
                result.backend,
                result.reason,
            )
            with self._lock:
                state = self._states[mac]
                state.mode = "error"
                state.backend = result.backend
                state.control_path = result.control_path
                state.backend_reason = result.reason
        return result.ok

    def _clamp_delay(self, delay_ms: float) -> float:
        return max(self.MIN_DELAY_MS, min(self.MAX_DELAY_MS, float(delay_ms)))


_ACTUATION_MANAGER: Optional[ActuationManager] = None


def get_actuation_manager() -> ActuationManager:
    global _ACTUATION_MANAGER
    if _ACTUATION_MANAGER is None:
        _ACTUATION_MANAGER = ActuationManager()
    return _ACTUATION_MANAGER
