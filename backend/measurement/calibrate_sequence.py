"""Slice 4.3 + Wi-Fi: sequential multi-speaker calibration (one BLE button).

Enumerates every ``syncsonic-delay-*.sock`` in ``/tmp/syncsonic-engine``,
skips the reserved-adapter (phone) MAC, then runs the Slice 4.2 pipeline
once per speaker while forwarding ``CALIBRATION_RESULT`` notifications.

If any Wi-Fi (Sonos) outputs are connected, the sequence first measures
the **Wi-Fi anchor lag** (mute every BT speaker, then either inject the
startup chirp or capture the user's existing music depending on the
calibration mode). The resulting lag becomes ``target_total_ms`` for
every BT speaker so they are pulled UP to match the Wi-Fi delay.

Anchor mode follows ``calibration_mode`` directly: the ``startup_tune``
button uses chirp anchoring + chirp BT calibration, and the ``music``
button uses music anchoring + music BT calibration. Each button is its
own deterministic path through the pipeline; there is no in-band
fallback between methods (the user picks the method by which button
they press).

Sonos muting strategy: the Sonos is muted **once at the sequence level**
before any BT calibration begins and kept muted through every BT leg.
This prevents audio that enters the Icecast pipeline during a BT
capture from reaching the Sonos speaker seconds later (the pipeline has
500-3000 ms of buffering). Per-capture mute/unmute cycling is too fast
and causes audible chirp echoes from the Sonos between calibration steps.

Failure handling: when a Wi-Fi speaker is connected and the anchor
measurement fails, the **whole sequence aborts** with a clear error.
Previously the code silently fell back to ``target_total_ms = 500`` for
the BT speakers, which actively destroyed any pre-existing alignment to
the Sonos. The user pressed Align expecting alignment, not a re-target
to a default that contradicts what they're hearing.

Payload sizing: the ``anchor_info`` block embedded in
``sequence_started`` and ``sequence_complete`` was pushing those
notifications past the BLE ATT MTU (~669 byte payload), causing BlueZ
to truncate them and the frontend's JSON.parse to fail. The frontend
already receives the full ``anchor_measured`` event before this, so the
nested anchor_info here is reduced to a minimal ``{anchor_mode,
anchor_lag_ms, reason}`` summary that fits comfortably under MTU.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from measurement.calibrate_one import (
    CALIBRATION_MODE_STARTUP_TUNE,
    CalibrationMode,
    DEFAULT_DURATION_SEC,
    DEFAULT_DURATION_STARTUP_SEC,
    DEFAULT_TARGET_TOTAL_MS,
    MAX_ADJUSTMENT_MS,
    MUTE_SETTLE_SEC,
    RAMP_MS,
    SOCK_DIR,
    _mute_sonos_devices,
    mute_bt_socket_for_mac,
    mute_bt_sockets_for_macs,
    calibrate_speaker_async,
)
from measurement.calibrate_anchor import (
    ANCHOR_MODE_CHIRP,
    ANCHOR_MODE_MUSIC,
    DEFAULT_CAPTURE_SEC as DEFAULT_ANCHOR_CAPTURE_SEC,
    AnchorMode,
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


def _anchor_max_adjustment(target_total_ms: float) -> float:
    """When the alignment target is large (Wi-Fi anchor case) we need to
    allow large single-step shifts on BT speakers; otherwise the per-call
    400 ms guard rejects every leg with ``adjustment_too_large``.

    The fall-back path also matters: if a user later disconnects every
    Sonos and triggers Align All again, the BT speakers will need to be
    pulled DOWN by ~anchor_lag, which is also a large single step.
    """
    return max(MAX_ADJUSTMENT_MS, float(target_total_ms) + 600.0)


def _slim_anchor_info(anchor_info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce the anchor measurement payload to MTU-safe essentials.

    The full anchor_info block returned by ``measure_wifi_anchor_lag``
    is ~450 bytes (it carries every diagnostic field — muted sockets,
    saved volumes, calibration_volume, full measurement dict, etc.).
    Embedded inside ``sequence_started`` / ``sequence_complete`` it
    pushes the wrapper notification past the BLE ATT MTU and BlueZ
    truncates it on the wire.

    The frontend already received the canonical ``anchor_measured``
    event before this and stored its anchor_lag_ms; here we only need
    to identify which anchor pass this sequence used + whether it
    succeeded. Everything else is recoverable from the prior event.
    """
    if not isinstance(anchor_info, dict):
        return {}
    keys = ("phase", "anchor_mode", "anchor_lag_ms", "reason")
    return {k: anchor_info[k] for k in keys if k in anchor_info}


def _anchor_mode_for(calibration_mode: CalibrationMode) -> AnchorMode:
    """The anchor uses the same signal type as the BT speaker pass.

    Each frontend button (startup tune vs music alignment) maps to one
    end-to-end path; the anchor is not allowed to silently fall back to
    a different signal type than the BT loop will use. This keeps the
    behaviour the user sees consistent with the button they pressed.
    """
    if calibration_mode == CALIBRATION_MODE_STARTUP_TUNE:
        return ANCHOR_MODE_CHIRP
    return ANCHOR_MODE_MUSIC


def calibrate_all_speakers_async(
    bus: Optional[Any],
    on_event: EventCallback,
    *,
    calibration_mode: CalibrationMode = CALIBRATION_MODE_STARTUP_TUNE,
    target_total_ms: float = DEFAULT_TARGET_TOTAL_MS,
    capture_duration_sec: Optional[float] = None,
    continue_on_failure: bool = True,
    wifi_device_ids: Optional[List[str]] = None,
) -> threading.Thread:
    """Spawn a daemon thread that calibrates each speaker sequentially.

    ``wifi_device_ids`` is the set of Sonos device IDs currently
    streaming via Icecast. When non-empty the sequence inserts an
    anchor-measurement step at the front: the Sonos delay becomes the
    alignment target for every BT leg, and the Sonos devices are
    SoCo-muted during each BT capture to keep their delayed echo out
    of the cross-correlation.

    Anchor mode is derived from ``calibration_mode``: ``startup_tune``
    → chirp anchor (deterministic, works without music); ``music`` →
    passive music anchor (sharper peak when audio is already playing).
    """

    wifi_ids = [d for d in (wifi_device_ids or []) if d]
    anchor_mode = _anchor_mode_for(calibration_mode)

    def _worker() -> None:
        macs = list_calibratable_macs(bus)
        if not macs and not wifi_ids:
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

        effective_target_ms = float(target_total_ms)
        anchor_info: Optional[Dict[str, Any]] = None
        if wifi_ids:
            try:
                from measurement.calibrate_anchor import measure_wifi_anchor_lag
            except ImportError as exc:
                on_event(
                    "sequence_failed",
                    {
                        "phase": "sequence_failed",
                        "reason": "anchor_module_unavailable",
                        "calibration_mode": calibration_mode,
                        "error": str(exc),
                    },
                )
                return
            anchor_lag, anchor_info = measure_wifi_anchor_lag(
                on_event,
                mode=anchor_mode,
                capture_duration_sec=max(float(dur), DEFAULT_ANCHOR_CAPTURE_SEC),
                wifi_device_ids=wifi_ids,
            )
            if anchor_lag is None:
                # CRITICAL: with Wi-Fi connected, we cannot fall back to a
                # BT-only target. Doing so would pull every BT speaker
                # to ~500 ms while the Sonos continues at ~5000 ms,
                # actively destroying the user's existing alignment. The
                # only correct response is to fail the whole sequence
                # and let the user retry.
                logger.warning(
                    "[anchor] measurement failed with Wi-Fi connected — aborting sequence",
                )
                on_event(
                    "sequence_failed",
                    {
                        "phase": "sequence_failed",
                        "calibration_mode": calibration_mode,
                        "anchor_mode": anchor_mode,
                        "reason": "anchor_failed_wifi_present",
                        "anchor_summary": _slim_anchor_info(anchor_info),
                    },
                )
                return
            effective_target_ms = float(anchor_lag)

        n = len(macs)
        summary: Dict[str, str] = {}

        on_event(
            "sequence_started",
            {
                "phase": "sequence_started",
                "calibration_mode": calibration_mode,
                "anchor_mode": anchor_mode if wifi_ids else None,
                "macs": list(macs),
                "sequence_total": n,
                "wifi_device_ids": list(wifi_ids),
                "target_total_ms": round(effective_target_ms, 2),
                "anchor_summary": _slim_anchor_info(anchor_info),
            },
        )

        # ── Sequence-level Sonos muting ──
        # Mute Sonos ONCE before any BT calibration and hold it muted for
        # the entire BT loop. The Icecast→Sonos pipeline has 500-3000 ms
        # of buffering; per-capture mute/unmute cycling is too short and
        # lets chirp audio bleed out of the Sonos between BT legs.
        sonos_muted_ids: List[str] = []
        if wifi_ids and macs:
            sonos_muted_ids = _mute_sonos_devices(wifi_ids, True)
            # Let the Icecast pipeline flush any residual chirp audio from
            # the anchor measurement. The settle time is based on the
            # measured (or worst-case) Sonos delay plus the chirp duration
            # so the chirp has fully exited the speaker before we start
            # recording BT speakers with the mic.
            settle_sec = (effective_target_ms / 1000.0) + 2.0
            if settle_sec < 4.0:
                settle_sec = 4.0
            logger.info("[sequence] Sonos muted; settling %.1f s for pipeline flush", settle_sec)
            time.sleep(settle_sec)

        max_adj = _anchor_max_adjustment(effective_target_ms)

        # ── Sequence-level BT isolation ──────────────────────────────────
        # Pre-mute EVERY BT speaker so the loop can unmute exactly one at
        # a time. This eliminates the ~500 ms window (unmute-others →
        # next-speaker-mutes-others) where all speakers were audible
        # simultaneously between measurement slots.
        logger.info(
            "[sequence] Pre-muting %d BT speakers for one-at-a-time isolation", len(macs)
        )
        mute_bt_sockets_for_macs(macs, muted=True)
        # Wait for all A2DP buffers to drain before the first measurement.
        # A small extra margin beyond MUTE_SETTLE_SEC ensures every speaker
        # is acoustically silent when speaker 1's capture window opens.
        time.sleep(MUTE_SETTLE_SEC + 0.2)

        try:
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

                # Unmute ONLY this speaker so it alone is audible while
                # _calibrate_blocking waits MUTE_SETTLE_SEC for the A2DP
                # buffer to refill, then captures it in isolation.
                mute_bt_socket_for_mac(mac, muted=False)

                # Do NOT pass extra_silence_devices here — Sonos is
                # already muted at the sequence level above.
                calibrate_speaker_async(
                    mac,
                    _cb,
                    target_total_ms=effective_target_ms,
                    capture_duration_sec=float(dur),
                    calibration_mode=calibration_mode,
                    sequence_index=idx,
                    sequence_total=n,
                    max_adjustment_ms=max_adj,
                    others_already_muted=True,
                )
                finished = done.wait(timeout=PER_SPEAKER_TIMEOUT_SEC)

                # Remute this speaker IMMEDIATELY, before the next one
                # is unmuted, so only one speaker is ever audible.
                mute_bt_socket_for_mac(mac, muted=True)

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
        finally:
            # Restore ALL BT speakers regardless of how the loop exited
            # so the user can hear music again immediately.
            logger.info("[sequence] Restoring all %d BT speakers after calibration loop", len(macs))
            mute_bt_sockets_for_macs(macs, muted=False)
            # ALWAYS restore Sonos, even on unexpected failure.
            if sonos_muted_ids:
                # Small settle so the last chirp clears the pipeline
                # before the Sonos becomes audible again.
                time.sleep(2.0)
                _mute_sonos_devices(sonos_muted_ids, False)
                logger.info("[sequence] Sonos unmuted after BT calibration loop")

        on_event(
            "sequence_complete",
            {
                "phase": "sequence_complete",
                "calibration_mode": calibration_mode,
                "anchor_mode": anchor_mode if wifi_ids else None,
                "macs": macs,
                "per_mac_outcome": summary,
                "target_total_ms": round(effective_target_ms, 2),
                "wifi_device_ids": list(wifi_ids),
                "anchor_summary": _slim_anchor_info(anchor_info),
            },
        )

    t = threading.Thread(target=_worker, name="syncsonic-calibrate-all", daemon=True)
    t.start()
    return t
