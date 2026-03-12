from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Dict, Any


@dataclass
class OutputActuatorState:
    mac: str
    mode: str = "idle"
    transport_delay_ms: float = 100.0
    delay_line_ms: float = 0.0
    target_delay_ms: float = 100.0
    applied_delay_ms: float = 100.0
    target_rate_ppm: float = 0.0
    applied_rate_ppm: float = 0.0
    relock_events: int = 0
    correction_events: int = 0
    updated_at: float = 0.0


class AlignmentActuatorEngine:
    """Persistent actuator state that applies control targets immediately.

    SyncSonic delay filters are intended to be set to explicit values and held.
    Any slew/relock loop here causes repeated transport rebuilds and audio
    instability. We therefore publish fixed delay/rate targets directly.
    """

    TRANSPORT_BASE_MS = 120.0

    def __init__(self) -> None:
        self._states: Dict[str, OutputActuatorState] = {}

    def step(
        self,
        targets: Dict[str, Dict[str, Any]],
        *,
        transport_base_ms: float = TRANSPORT_BASE_MS,
    ) -> Dict[str, Dict[str, Any]]:
        now = time.monotonic()
        transport_base_ms = max(self.TRANSPORT_BASE_MS, float(transport_base_ms))
        current_macs = {mac.upper() for mac in targets.keys()}
        for mac in list(self._states.keys()):
            if mac not in current_macs:
                self._states.pop(mac, None)

        snapshots: Dict[str, Dict[str, Any]] = {}
        for mac, raw in targets.items():
            mac = mac.upper()
            state = self._states.get(mac)
            if state is None:
                target_delay = self._clamp_delay(float(raw.get("delay_ms", transport_base_ms)), transport_base_ms)
                target_rate = float(raw.get("rate_ppm", 0.0))
                state = OutputActuatorState(
                    mac=mac,
                    mode=str(raw.get("mode", "idle")),
                    transport_delay_ms=transport_base_ms,
                    delay_line_ms=max(0.0, target_delay - transport_base_ms),
                    target_delay_ms=target_delay,
                    applied_delay_ms=target_delay,
                    target_rate_ppm=target_rate,
                    applied_rate_ppm=target_rate,
                    updated_at=now,
                )
                self._states[mac] = state
                snapshots[mac] = asdict(state)
                continue

            self._apply_target(state, raw, transport_base_ms)
            state.updated_at = now
            snapshots[mac] = asdict(state)

        return snapshots

    def _apply_target(
        self,
        state: OutputActuatorState,
        raw: Dict[str, Any],
        transport_base_ms: float,
    ) -> None:
        state.mode = str(raw.get("mode", state.mode))
        state.transport_delay_ms = max(self.TRANSPORT_BASE_MS, float(transport_base_ms))
        target_delay = self._clamp_delay(float(raw.get("delay_ms", state.target_delay_ms)), state.transport_delay_ms)
        target_rate = float(raw.get("rate_ppm", state.target_rate_ppm))
        state.target_delay_ms = target_delay
        state.target_rate_ppm = target_rate
        changed = (
            abs(target_delay - state.applied_delay_ms) >= 0.001
            or abs(target_rate - state.applied_rate_ppm) >= 0.001
        )
        state.applied_delay_ms = self._clamp_delay(target_delay, state.transport_delay_ms)
        state.delay_line_ms = max(0.0, state.applied_delay_ms - state.transport_delay_ms)
        state.applied_rate_ppm = target_rate
        if changed:
            state.correction_events += 1

    def _clamp_delay(self, value: float, transport_base_ms: float) -> float:
        return max(float(transport_base_ms), float(value))
