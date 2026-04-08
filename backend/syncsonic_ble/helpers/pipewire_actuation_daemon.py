from __future__ import annotations

import sys
import time
from typing import Any, Dict

from syncsonic_ble.helpers.alignment_actuator import AlignmentActuatorEngine
from syncsonic_ble.helpers.pipewire_control_plane import read_control_state
from syncsonic_ble.helpers.pipewire_transport import get_pipewire_transport_manager
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

POLL_INTERVAL_SEC = 0.25


class PipeWireActuationDaemon:
    """Persistent runtime that maintains manual delay-node routes."""

    def __init__(self) -> None:
        self._engine = AlignmentActuatorEngine()
        self._transport = get_pipewire_transport_manager()
        self._last_outputs: Dict[str, Dict[str, Any]] = {}

    def run_forever(self) -> None:
        log.info("PipeWire actuation daemon started")
        while True:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("PipeWire actuation daemon tick failed: %s", exc)
            time.sleep(POLL_INTERVAL_SEC)

    def _tick(self) -> None:
        state = read_control_state()
        outputs = state.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}

        current: Dict[str, Dict[str, Any]] = {}
        for mac, raw in outputs.items():
            if not isinstance(raw, dict):
                continue
            active = bool(raw.get("active", True))
            current[mac.upper()] = {
                "delay_ms": float(raw.get("delay_ms", 100.0)),
                "rate_ppm": 0.0,
                "mode": "manual" if active else "idle",
                "active": active,
                "left_percent": raw.get("left_percent"),
                "right_percent": raw.get("right_percent"),
            }

        applied_states = self._engine.step(current)

        for mac, cfg in current.items():
            if not cfg["active"]:
                self._transport.remove_route(mac)
                continue

            applied = applied_states.get(mac, {})
            self._transport.ensure_route(
                mac,
                latency_ms=float(applied.get("delay_line_ms", 0.0)),
                left_percent=int(cfg["left_percent"]) if cfg.get("left_percent") is not None else None,
                right_percent=int(cfg["right_percent"]) if cfg.get("right_percent") is not None else None,
            )

        for mac in list(self._last_outputs.keys()):
            if mac not in current:
                self._transport.remove_route(mac)

        self._last_outputs = current


def main() -> None:
    daemon = PipeWireActuationDaemon()
    daemon.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
