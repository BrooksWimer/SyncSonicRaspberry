from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import Lock
from typing import Dict, Optional


@dataclass
class AlignmentControllerState:
    mac: str
    mode: str = "acquire"
    filtered_offset_ms: float = 0.0
    integral_error_sec: float = 0.0
    target_ratio_ppm: float = 0.0
    last_measured_offset_ms: float = 0.0
    correction_events: int = 0
    relock_events: int = 0


@dataclass
class AlignmentPlan:
    reference_mac: str
    target_mac: str
    measured_offset_ms: float
    filtered_offset_ms: float
    controller_mode: str
    target_ratio_ppm: float
    action: str
    adjusted_mac: Optional[str] = None
    target_delay_ms: Optional[float] = None
    correction_step_ms: float = 0.0
    relock: bool = False

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


class AlignmentController:
    """PLL-like controller for acoustic alignment planning.

    This owns the decision layer for auto-sync:
    - low-pass filtering of acoustic error
    - acquisition vs lock mode
    - bounded rate-target telemetry in ppm
    - coarse correction plans (slew or relock)

    It deliberately does not know how audio is actuated.
    """

    OFFSET_TOLERANCE_MS = 2.0

    def __init__(self) -> None:
        self._lock = Lock()
        self._states: Dict[str, AlignmentControllerState] = {}

    def plan_relative_correction(
        self,
        *,
        reference_mac: str,
        target_mac: str,
        measured_offset_ms: float,
        reference_delay_ms: float,
        target_delay_ms: float,
    ) -> AlignmentPlan:
        reference_mac = reference_mac.upper()
        target_mac = target_mac.upper()
        state = self._ensure_state(target_mac)

        with self._lock:
            # The current actuator is delay-only. Use the fresh acoustic
            # measurement directly so successive sync runs do not chase a stale
            # filtered error and keep overcorrecting.
            filtered_offset_ms = measured_offset_ms
            magnitude_ms = abs(filtered_offset_ms)
            controller_mode = "lock" if magnitude_ms <= self.OFFSET_TOLERANCE_MS else "acquire"
            integral_error_sec = 0.0
            ratio_ppm = 0.0

            state.mode = controller_mode
            state.filtered_offset_ms = filtered_offset_ms
            state.integral_error_sec = integral_error_sec
            state.target_ratio_ppm = ratio_ppm
            state.last_measured_offset_ms = measured_offset_ms

            if magnitude_ms <= self.OFFSET_TOLERANCE_MS:
                return AlignmentPlan(
                    reference_mac=reference_mac,
                    target_mac=target_mac,
                    measured_offset_ms=measured_offset_ms,
                    filtered_offset_ms=filtered_offset_ms,
                    controller_mode=controller_mode,
                    target_ratio_ppm=ratio_ppm,
                    action="rate_only",
                )

            adjusted_mac = reference_mac if filtered_offset_ms > 0 else target_mac
            relock = True
            correction_step_ms = magnitude_ms
            if adjusted_mac == target_mac:
                planned_delay_ms = target_delay_ms + correction_step_ms
            else:
                planned_delay_ms = reference_delay_ms + correction_step_ms
            state.correction_events += 1
            state.relock_events += 1

            return AlignmentPlan(
                reference_mac=reference_mac,
                target_mac=target_mac,
                measured_offset_ms=measured_offset_ms,
                filtered_offset_ms=filtered_offset_ms,
                controller_mode=controller_mode,
                target_ratio_ppm=ratio_ppm,
                action="relock",
                adjusted_mac=adjusted_mac,
                target_delay_ms=planned_delay_ms,
                correction_step_ms=correction_step_ms,
                relock=relock,
            )

    def get_state_snapshot(self, mac: Optional[str] = None):
        with self._lock:
            if mac is not None:
                state = self._states.get(mac.upper())
                return asdict(state) if state else None
            return {device_mac: asdict(state) for device_mac, state in self._states.items()}

    def _ensure_state(self, mac: str) -> AlignmentControllerState:
        mac = mac.upper()
        with self._lock:
            state = self._states.get(mac)
            if state is None:
                state = AlignmentControllerState(mac=mac)
                self._states[mac] = state
            return state

_ALIGNMENT_CONTROLLER: Optional[AlignmentController] = None


def get_alignment_controller() -> AlignmentController:
    global _ALIGNMENT_CONTROLLER
    if _ALIGNMENT_CONTROLLER is None:
        _ALIGNMENT_CONTROLLER = AlignmentController()
    return _ALIGNMENT_CONTROLLER
