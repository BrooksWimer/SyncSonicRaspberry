"""Slice 4.3: sequential multi-speaker calibration (one BLE button).

Enumerates every ``syncsonic-delay-*.sock`` in ``/tmp/syncsonic-engine``,
skips the reserved-adapter (phone) MAC, then runs the Slice 4.2 pipeline
once per speaker while forwarding ``CALIBRATION_RESULT`` notifications.

Default mode is ``startup_tune`` so each leg hears the known chirp rather
than relying on whatever music is playing.

Wi-Fi outputs follow later: once they participate in the same filter /
sink layout as Bluetooth, they appear in the same enumeration surface.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from measurement.calibrate_one import (
    CALIBRATION_MODE_STARTUP_TUNE,
    CalibrationMode,
    DEFAULT_DURATION_SEC,
    DEFAULT_DURATION_STARTUP_SEC,
    DEFAULT_TARGET_TOTAL_MS,
    SOCK_DIR,
    calibrate_speaker_async,
)
from syncsonic_ble.helpers.adapter_helpers import is_device_on_reserved_adapter
from syncsonic_ble.utils.logging_conf import get_logger

logger = get_logger(__name__)

EventCallback = Callable[[str, Dict[str, Any]], None]

PER_SPEAKER_TIMEOUT_SEC = 180.0


def _mac_from_sock_stem(stem: str) -> Optional[str]:
    # stem like syncsonic-delay-f4_6a_dd_d4_f3_c8 (no .sock)
    prefix = "syncsonic-delay-"
    if not stem.startswith(prefix):
        return None
    body = stem[len(prefix) :]
    octets = body.split("_")
    if len(octets) != 6:
        return None
    return ":".join(o.upper() for o in octets)


def list_calibratable_macs(bus: Optional[Any]) -> List[str]:
    """MACs that have a delay-filter socket and are not the phone."""
    out: List[str] = []
    if not SOCK_DIR.exists():
        return out
    for path in sorted(SOCK_DIR.glob("syncsonic-delay-*.sock")):
        mac = _mac_from_sock_stem(path.stem)
        if not mac:
            continue
        if bus is not None:
            try:
                if is_device_on_reserved_adapter(bus, mac):
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.debug("reserved-adapter check failed for %s: %s", mac, exc)
                continue
        out.append(mac)
    return out


def calibrate_all_speakers_async(
    bus: Optional[Any],
    on_event: EventCallback,
    *,
    calibration_mode: CalibrationMode = CALIBRATION_MODE_STARTUP_TUNE,
    target_total_ms: float = DEFAULT_TARGET_TOTAL_MS,
    capture_duration_sec: Optional[float] = None,
    continue_on_failure: bool = True,
) -> threading.Thread:
    """Spawn a daemon thread that calibrates each speaker sequentially."""

    def _worker() -> None:
        macs = list_calibratable_macs(bus)
        if not macs:
            on_event(
                "sequence_failed",
                {
                    "phase": "sequence_failed",
                    "reason": "no_calibratable_outputs",
                    "calibration_mode": calibration_mode,
                },
            )
            return

        dur = capture_duration_sec
        if dur is None:
            dur = (
                DEFAULT_DURATION_STARTUP_SEC
                if calibration_mode == CALIBRATION_MODE_STARTUP_TUNE
                else DEFAULT_DURATION_SEC
            )

        n = len(macs)
        summary: Dict[str, str] = {}

        on_event(
            "sequence_started",
            {
                "phase": "sequence_started",
                "calibration_mode": calibration_mode,
                "macs": list(macs),
                "sequence_total": n,
            },
        )

        for idx, mac in enumerate(macs, start=1):
            done = threading.Event()

            def _cb(
                phase: str,
                payload: Dict[str, Any],
                _done: threading.Event = done,
                _mac: str = mac,
            ) -> None:
                on_event(phase, payload)
                if phase in ("applied", "failed"):
                    summary[_mac] = phase
                    _done.set()

            calibrate_speaker_async(
                mac,
                _cb,
                target_total_ms=target_total_ms,
                capture_duration_sec=float(dur),
                calibration_mode=calibration_mode,
                sequence_index=idx,
                sequence_total=n,
            )
            finished = done.wait(timeout=PER_SPEAKER_TIMEOUT_SEC)
            if not finished:
                summary[mac] = "timeout"
                on_event(
                    "failed",
                    {
                        "phase": "failed",
                        "mac": mac,
                        "reason": "speaker_calibration_timeout",
                        "sequence_index": idx,
                        "sequence_total": n,
                        "calibration_mode": calibration_mode,
                    },
                )
                if not continue_on_failure:
                    break

        on_event(
            "sequence_complete",
            {
                "phase": "sequence_complete",
                "calibration_mode": calibration_mode,
                "macs": macs,
                "per_mac_outcome": summary,
            },
        )

    t = threading.Thread(target=_worker, name="syncsonic-calibrate-all", daemon=True)
    t.start()
    return t
