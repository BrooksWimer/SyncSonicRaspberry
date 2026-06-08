"""Arrival burst actuator -- Option C: in-filter burst emission.

Issues emit_burst + query_emit_timestamps socket commands to the
pw_delay_filter process for the target speaker. The filter synthesizes
the burst inside its audio callback and timestamps it frame-precisely.

This replaces the paplay-based v1 path retired on 2026-05-25.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from syncsonic_ble.helpers.pipewire_transport import get_pipewire_transport_manager
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

DEFAULT_FREQ_HZ = 18_500.0
DEFAULT_DUR_MS = 100
DEFAULT_AMPLITUDE = 0.95
EMIT_SETTLE_SEC = 0.025
SAMPLE_RATE = 48_000


class ArrivalBurstActuator:
    def emit_once(
        self,
        mac: str,
        *,
        freq_hz: float = DEFAULT_FREQ_HZ,
        duration_ms: int = DEFAULT_DUR_MS,
        amplitude: float = DEFAULT_AMPLITUDE,
    ) -> List[Dict[str, Any]]:
        """Request one burst; return emit timestamp entries from the filter."""
        tm = get_pipewire_transport_manager()
        query = tm.query_filter(mac) or {}
        delay_samples = int(query.get("target_delay_samples") or 0)
        ack = tm.emit_burst(mac, freq_hz=freq_hz, duration_ms=duration_ms, amplitude=amplitude)
        if ack is None or not ack.get("ok"):
            log.warning("emit_burst socket command failed for %s: %s", mac, ack)
            return []
        deadline = time.monotonic() + (delay_samples / SAMPLE_RATE) + (duration_ms / 1000.0) + 0.25
        while True:
            time.sleep(EMIT_SETTLE_SEC)
            result = tm.query_emit_timestamps(mac)
            if result is None:
                log.warning("query_emit_timestamps failed for %s", mac)
                return []
            entries = result.get("entries", [])
            if isinstance(entries, list) and entries:
                return entries
            if time.monotonic() >= deadline:
                return []


_ACTUATOR: Optional[ArrivalBurstActuator] = None


def get_arrival_burst_actuator() -> ArrivalBurstActuator:
    global _ACTUATOR
    if _ACTUATOR is None:
        _ACTUATOR = ArrivalBurstActuator()
    return _ACTUATOR
