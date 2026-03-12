from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Dict, Any

from syncsonic_ble.helpers.alignment_actuator import AlignmentActuatorEngine
from syncsonic_ble.helpers.pipewire_control_plane import read_control_state
from syncsonic_ble.helpers.pipewire_dsp_contract import write_dsp_state
from syncsonic_ble.helpers.pipewire_transport import get_pipewire_transport_manager
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

METADATA_NAME = "default"
METADATA_ID = "0"
METADATA_KEY_PREFIX = "syncsonic.output."
POLL_INTERVAL_SEC = 0.25


class PipeWireActuationDaemon:
    """Persistent PipeWire-side runtime for SyncSonic output actuation.

    Responsibilities:
    - watch the SyncSonic control-plane state file
    - keep one native pw-loopback transport per active output
    - publish per-output delay/rate targets into PipeWire metadata

    This is the PipeWire-native runtime side of the redesign. SyncSonic writes
    control targets; this daemon owns the long-lived graph/runtime machinery.
    """

    def __init__(self) -> None:
        self._engine = AlignmentActuatorEngine()
        self._transport = get_pipewire_transport_manager()
        self._last_outputs: Dict[str, Dict[str, Any]] = {}

    def run_forever(self) -> None:
        log.info("PipeWire actuation daemon started")
        while True:
            try:
                self._tick()
            except Exception as exc:
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
            current[mac.upper()] = raw

        applied_states = self._engine.step(current)
        dsp_outputs: Dict[str, Dict[str, Any]] = {}

        for mac, cfg in current.items():
            active = bool(cfg.get("active", True))
            if not active:
                self._transport.remove_route(mac)
                self._delete_metadata(mac, applied=False)
                self._delete_metadata(mac, applied=True)
                continue

            applied = applied_states.get(mac, {})
            stage_delay_ms = float(applied.get("delay_line_ms", 0.0))
            left_percent = cfg.get("left_percent")
            right_percent = cfg.get("right_percent")
            self._transport.ensure_route(
                mac,
                latency_ms=stage_delay_ms,
                left_percent=int(left_percent) if left_percent is not None else None,
                right_percent=int(right_percent) if right_percent is not None else None,
            )
            transport_delay_ms = float(applied.get("transport_delay_ms", 120.0))
            dsp_outputs[mac] = {
                "mode": str(applied.get("mode", cfg.get("mode", "idle"))),
                "transport_delay_ms": round(transport_delay_ms, 3),
                "delay_line_ms": round(float(applied.get("delay_line_ms", 0.0)), 3),
                "applied_rate_ppm": round(float(applied.get("applied_rate_ppm", cfg.get("rate_ppm", 0.0))), 3),
                "target_delay_ms": round(float(cfg.get("delay_ms", 100.0)), 3),
                "target_rate_ppm": round(float(cfg.get("rate_ppm", 0.0)), 3),
                "relock_events": int(applied.get("relock_events", 0)),
                "correction_events": int(applied.get("correction_events", 0)),
            }
            self._publish_metadata(
                mac,
                delay_ms=float(cfg.get("delay_ms", 100.0)),
                rate_ppm=float(cfg.get("rate_ppm", 0.0)),
                mode=str(cfg.get("mode", "idle")),
                applied=False,
            )
            self._publish_metadata(
                mac,
                delay_ms=float(applied.get("applied_delay_ms", cfg.get("delay_ms", 100.0))),
                rate_ppm=float(applied.get("applied_rate_ppm", cfg.get("rate_ppm", 0.0))),
                mode=str(applied.get("mode", cfg.get("mode", "idle"))),
                applied=True,
            )

        for mac in list(self._last_outputs.keys()):
            if mac not in current:
                self._transport.remove_route(mac)
                self._delete_metadata(mac, applied=False)
                self._delete_metadata(mac, applied=True)

        write_dsp_state(dsp_outputs)
        self._last_outputs = current

    def _publish_metadata(self, mac: str, *, delay_ms: float, rate_ppm: float, mode: str, applied: bool) -> None:
        key = f"{METADATA_KEY_PREFIX}{'applied.' if applied else ''}{mac}"
        value = json.dumps(
            {
                "delay_ms": round(delay_ms, 3),
                "rate_ppm": round(rate_ppm, 3),
                "mode": mode,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, METADATA_ID, key, value, "Spa:String:JSON"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _delete_metadata(self, mac: str, *, applied: bool) -> None:
        key = f"{METADATA_KEY_PREFIX}{'applied.' if applied else ''}{mac}"
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, "-d", METADATA_ID, key],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main() -> None:
    daemon = PipeWireActuationDaemon()
    daemon.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
