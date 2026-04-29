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
# When ensure_route fails because the bluez_output sink isn't present (the
# typical failure mode for a disconnected or about-to-reconnect speaker),
# don't retry for OFFLINE_BACKOFF_SEC. This eliminates the 410 ms
# transport-sink-not-found warning storm previously observed on the Pi when a
# control-state entry outlived its underlying sink. The first failure per
# offline period is still logged exactly once at INFO so the operator sees
# "this MAC went offline" without spam.
OFFLINE_BACKOFF_SEC = 5.0


class PipeWireActuationDaemon:
    """Persistent runtime that maintains manual delay-node routes."""

    def __init__(self) -> None:
        self._engine = AlignmentActuatorEngine()
        self._transport = get_pipewire_transport_manager()
        self._last_outputs: Dict[str, Dict[str, Any]] = {}
        self._offline_until: Dict[str, float] = {}

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
        now = time.monotonic()

        for mac, cfg in current.items():
            if not cfg["active"]:
                self._transport.remove_route(mac)
                self._offline_until.pop(mac, None)
                continue

            offline_until = self._offline_until.get(mac, 0.0)
            if now < offline_until:
                # Backoff window active; skip the ensure_route call entirely so
                # we neither do the work nor produce a log line.
                continue

            applied = applied_states.get(mac, {})
            ok = self._transport.ensure_route(
                mac,
                latency_ms=float(applied.get("delay_line_ms", 0.0)),
                left_percent=int(cfg["left_percent"]) if cfg.get("left_percent") is not None else None,
                right_percent=int(cfg["right_percent"]) if cfg.get("right_percent") is not None else None,
            )
            if ok:
                if mac in self._offline_until:
                    log.info("Speaker %s back online; route re-established", mac)
                    self._offline_until.pop(mac, None)
            else:
                if mac not in self._offline_until:
                    log.info(
                        "Speaker %s appears offline; backing off route attempts for %.1fs",
                        mac,
                        OFFLINE_BACKOFF_SEC,
                    )
                self._offline_until[mac] = now + OFFLINE_BACKOFF_SEC

        for mac in list(self._last_outputs.keys()):
            if mac not in current:
                self._transport.remove_route(mac)
                self._offline_until.pop(mac, None)

        self._last_outputs = current


def main() -> None:
    daemon = PipeWireActuationDaemon()
    daemon.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
