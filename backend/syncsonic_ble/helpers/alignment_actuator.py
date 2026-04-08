from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Dict


@dataclass
class OutputActuatorState:
    mac: str
    mode: str = "idle"
    transport_delay_ms: float = 120.0
    delay_line_ms: float = 0.0
    target_delay_ms: float = 100.0
    applied_delay_ms: float = 100.0
    target_rate_ppm: float = 0.0
    applied_rate_ppm: float = 0.0
    relock_events: int = 0
    correction_events: int = 0
    updated_at: float = 0.0


class AlignmentActuatorEngine:
    """Translate manual delay targets into transport-base plus delay-line delay."""

    TRANSPORT_BASE_MS = 120.0
    MAX_DELAY_MS = 4000.0

    def __init__(self) -> None:
        self._states: Dict[str, OutputActuatorState] = {}

    def step(self, targets: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        current_macs = {mac.upper() for mac in targets.keys()}
        for mac in list(self._states.keys()):
            if mac not in current_macs:
                self._states.pop(mac, None)

        snapshots: Dict[str, Dict[str, Any]] = {}
        for mac, raw in targets.items():
            mac = mac.upper()
            target_delay = self._clamp_delay(float(raw.get("delay_ms", self.TRANSPORT_BASE_MS)))
            state = self._states.get(mac)
            if state is None:
                state = OutputActuatorState(mac=mac)
                self._states[mac] = state

            state.mode = str(raw.get("mode", "manual"))
            state.transport_delay_ms = self.TRANSPORT_BASE_MS
            state.target_delay_ms = target_delay
            state.applied_delay_ms = target_delay
            state.delay_line_ms = max(0.0, target_delay - self.TRANSPORT_BASE_MS)
            state.target_rate_ppm = 0.0
            state.applied_rate_ppm = 0.0
            state.relock_events = 0
            state.correction_events = 0
            state.updated_at = time.monotonic()
            snapshots[mac] = asdict(state)

        return snapshots

    def _clamp_delay(self, value: float) -> float:
        return max(self.TRANSPORT_BASE_MS, min(self.MAX_DELAY_MS, float(value)))
