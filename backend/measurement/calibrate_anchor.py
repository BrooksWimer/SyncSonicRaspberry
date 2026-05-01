"""Wi-Fi anchor measurement: measure the Sonos / Icecast acoustic lag.

Wi-Fi (Sonos) outputs are inherently the slowest leg in a hybrid
configuration (Icecast queueing + UPnP buffer + LAN jitter routinely
exceeds 3-5 s on consumer Sonos hardware). They become the alignment
**anchor**: every BT speaker is pulled UP to match the Sonos delay
rather than the other way around (we cannot give an output negative
latency).

Two reference signals — chirp and music — selected by ``mode``:

- ``mode="chirp"``: paplay the synthesised startup-tune WAV into
  ``virtual_out`` during the capture. Deterministic, works even when
  the user's phone audio is paused. The cross-correlation peak is
  fuzzier through MP3 + Sonos DSP, so the confidence thresholds are
  loosened. Used by the "startup tune" alignment button.
- ``mode="music"``: capture passively while the user's existing
  music flows through ``virtual_out``. Music is broadband and full
  of unique transients, so the cross-correlation peak is much sharper
  than a chirp through the same MP3 chain (~10-12x SNR observed on
  the dev unit). Requires real audio on ``virtual_out``; we reject
  with ``no_audio_playing`` if the reference capture is silent. Used
  by the "music alignment" button.

The mode is chosen by the caller. ``calibrate_sequence`` derives it
directly from the user-facing calibration mode (startup_tune→chirp,
music→music) so each frontend button has a deterministic path
through the entire calibration pipeline with no in-band fallback.

Flow:

  1. Mute every BT delay-filter so the mic only picks up the Sonos
     speaker. Wait ``BT_SETTLE_SEC`` for the BT speaker's internal
     buffer to drain — without this the first second of the capture
     contains residual BT audio that contaminates the correlation.
  2. Save and boost the Sonos volume so the chirp/music dominates
     ambient noise at the mic.
  3. Concurrently parecord ``virtual_out.monitor`` and the Jieli mic
     for ``capture_duration_sec`` seconds. In ``chirp`` mode also
     paplay the chirp WAV into ``virtual_out`` during the capture.
  4. (Music mode only) Validate the reference has audio energy. If
     ``virtual_out`` is silent return ``no_audio_playing`` so the
     caller can prompt the user to start music or use chirp mode.
  5. Cross-correlate over [ANCHOR_MIN_LAG_MS, ANCHOR_MAX_LAG_MS] ms.
  6. Restore BT mutes and Sonos volume.
  7. Return the measured anchor lag in ms.

This module is deliberately small and stateless. It reuses
``calibrate_one._capture_pair`` for the parecord plumbing and the
existing C-filter mute_to socket layer. It does **not** publish any
delay - the multi-speaker sequence does that for the BT outputs based
on the value we return.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from syncsonic_ble.utils.logging_conf import get_logger

from measurement.calibrate_one import (
    CAPTURE_DIR,
    RAMP_MS,
    _capture_pair,
    _send_filter_command,
)
from measurement.startup_tune import ensure_startup_tune_wav

logger = get_logger(__name__)

AnchorMode = Literal["chirp", "music"]
ANCHOR_MODE_CHIRP: AnchorMode = "chirp"
ANCHOR_MODE_MUSIC: AnchorMode = "music"

# Physically plausible Sonos lag bounds. The chain is
# ``ffmpeg → libshout → Icecast queue → Sonos UPnP buffer → speaker DSP →
# air → mic``. Even with a minimal Icecast queue, the Sonos UPnP buffer
# alone is typically 1500-3000 ms; total can reach 5+ s on consumer
# hardware (4876-5066 ms measured on the dev Sonos across multiple runs).
#
# Minimum floor raised from 300 ms to 2000 ms in 2026-05-01 robustness
# pass. Music cross-correlation can find a ghost peak at 1742 ms when
# the song has a repeating pattern (drum loop, ostinato, etc.) at that
# offset. The 1742 ms ghost has higher correlation energy in some 10 s
# windows than the real 4876 ms peak, so without a physical minimum the
# analyzer accepts the wrong lag and the subsequent BT calibration tries
# to pull speakers to a 1742 ms target (then fails with adjustment_too_large,
# which is safe but confusing). 2000 ms is a safe conservative minimum:
# no realistic Sonos/Icecast deployment has end-to-end lag under 1.5 s.
#   - peaks below 2000 ms: music ghost or early reflection, not the Sonos
#   - peaks above 6500 ms: room reflection or correlation-tail sidelobe
ANCHOR_MIN_LAG_MS = 2000.0
ANCHOR_MAX_LAG_MS = 6500.0

# Capture window. With chirp duration 2.65 s + max-lag 6.5 s + tail
# margin we need at least ~9.5 s to give the cross-correlator a clean
# overlap region at every plausible lag. 12 s gives 2.5 s of safety.
# Same window for music mode keeps both code paths uniform and lets
# music mode benefit from the longer averaging.
DEFAULT_CAPTURE_SEC = 12.0

# Wait this long after BT mute_to-zero before starting the capture.
# Without it the first second of the recording contains residual BT
# audio from the codec/RF/speaker DSP chain that contaminates the
# cross-correlation against virtual_out (the mic hears BT music that
# virtual_out emitted seconds ago, producing a spurious peak near the
# BT-only lag and outranking the real Sonos peak, especially in chirp
# mode where the Sonos peak is already smeared by MP3).
BT_SETTLE_SEC = 1.5

# Sonos volume the calibration drives the speaker to during the
# measurement (0-100). High enough that the reference dominates ambient
# noise at the mic; restored to the user's prior value afterwards.
# A debug capture at the user's default 50 % showed mic peak ≈ 0.012
# (vs ref 0.22 — about 24× quieter) which let noise sidelobes outrank
# the real Sonos peak. 85 % gives the calibration ~5 dB more SNR while
# staying short of "blast the room" territory; the boost lasts only
# ~13 s before the original volume is restored.
ANCHOR_SONOS_VOLUME = 85

# Confidence thresholds, per mode.
#
# Music mode threshold is 1.0 (not the BT-speaker 1.2).
#
# Real-world evidence from 2026-05-01: two back-to-back music-anchor
# measurements returned lag_ms=4924 (conf_secondary=1.15) and
# lag_ms=4876 (conf_secondary=2.08). The lags agree to 48 ms; the
# first was correct but rejected at 1.2. Mid-tempo music with a
# drum loop creates a ghost correlation peak ~500 ms away from the
# real peak that can push secondary confidence below 1.2 even when
# the primary peak is unambiguous.
#
# The ref_rms gate (>> MIN_REF_RMS_MUSIC=100) already ensures real
# signal is present, so a secondary of 1.0 means "the direct-path
# peak is at least as strong as the next-best ghost" — sufficient
# given that the ghost is a musical repeat 500 ms away, not noise.
#
# Chirp mode stays at 0.9: MP3 + Sonos DSP smear the chirp's auto-
# correlation enough that the anchor capture frequently lands in the
# 0.9-1.1 range with the lag still correct.
ANCHOR_MIN_CONFIDENCE_PRIMARY_CHIRP = 2.0
ANCHOR_MIN_CONFIDENCE_SECONDARY_CHIRP = 0.9
ANCHOR_MIN_CONFIDENCE_PRIMARY_MUSIC = 3.0
ANCHOR_MIN_CONFIDENCE_SECONDARY_MUSIC = 1.0

# Music-mode reference-energy floor. ``load_wav_mono`` returns int16
# values cast to float64, so the natural unit is the int16 sample
# range [-32768, 32767]. RMS ≈ 100 corresponds to roughly -50 dB FS:
# comfortably above the float64 numerical / DC-bias floor we would
# see when virtual_out has no real audio, and well below any normal
# music passage (even quiet fade-outs run in the ~500-2000 range).
MIN_REF_RMS_MUSIC = 100.0


def _all_bt_sockets() -> List[Path]:
    """Every BT speaker socket. Same enumeration as ``_other_sockets``
    used in the per-speaker path, but for "every" speaker."""
    sock_dir = Path("/tmp/syncsonic-engine")
    if not sock_dir.exists():
        return []
    return sorted(sock_dir.glob("syncsonic-delay-*.sock"))


def _boost_sonos_volumes(
    wifi_device_ids: List[str], target_volume: int,
) -> Dict[str, int]:
    """Set every Sonos in *wifi_device_ids* to *target_volume*, returning a
    ``{device_id: previous_volume}`` map so the caller can restore exactly
    what the user had set before the calibration. Devices we can't read or
    set are skipped silently — best-effort, the calibration still proceeds
    with whatever volume the speaker happens to be at.
    """
    saved: Dict[str, int] = {}
    if not wifi_device_ids:
        return saved
    try:
        from syncsonic_ble.helpers import sonos_controller  # type: ignore
    except ImportError:
        return saved
    for d in wifi_device_ids:
        try:
            prev = sonos_controller.get_volume(d)
            if prev is None:
                continue
            if sonos_controller.set_volume(d, int(target_volume)):
                saved[d] = int(prev)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[anchor] volume save/set failed for %s: %s", d, exc)
    return saved


def _restore_sonos_volumes(saved: Dict[str, int]) -> None:
    if not saved:
        return
    try:
        from syncsonic_ble.helpers import sonos_controller  # type: ignore
    except ImportError:
        return
    for d, prev in saved.items():
        try:
            sonos_controller.set_volume(d, int(prev))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[anchor] volume restore failed for %s -> %d: %s",
                           d, prev, exc)


def measure_wifi_anchor_lag(
    on_event: Callable[[str, Dict[str, Any]], None],
    *,
    mode: AnchorMode = ANCHOR_MODE_CHIRP,
    capture_duration_sec: float = DEFAULT_CAPTURE_SEC,
    wifi_device_ids: Optional[List[str]] = None,
) -> Tuple[Optional[float], Dict[str, Any]]:
    """Run a single anchor measurement. Returns ``(lag_ms or None, info)``.

    ``info`` always contains the measurement attempt details so the
    caller can emit a rich BLE event regardless of success/failure.

    ``mode`` selects the reference signal:
      - ``"chirp"``: inject the startup-tune WAV during capture
      - ``"music"``: passively capture whatever is on virtual_out

    ``wifi_device_ids`` lets the anchor temporarily push every Sonos to
    a high calibration volume for the duration of the capture, then
    restore the user's previous volume. SNR at the mic is the limiting
    factor for anchor accuracy; this is the largest single lever.
    """
    wifi_ids = list(wifi_device_ids or [])
    info: Dict[str, Any] = {
        "phase": "anchor_measuring",
        "wifi_device_ids": list(wifi_ids),
        "anchor_mode": mode,
    }
    on_event("anchor_measuring", dict(info))

    tune_path: Optional[Path] = None
    if mode == ANCHOR_MODE_CHIRP:
        try:
            tune_path = ensure_startup_tune_wav()
        except Exception as exc:  # noqa: BLE001
            info.update({"phase": "anchor_failed", "reason": "startup_tune_unavailable",
                         "error": repr(exc)})
            on_event("anchor_failed", dict(info))
            return None, info

    bt_socks = _all_bt_sockets()
    info["muted_bt_socks"] = [p.name for p in bt_socks]
    for sock in bt_socks:
        if not _send_filter_command(sock, f"mute_to 0 {RAMP_MS}"):
            logger.warning("[anchor] mute_to failed for %s", sock)

    saved_volumes = _boost_sonos_volumes(wifi_ids, ANCHOR_SONOS_VOLUME)
    info["saved_volumes"] = dict(saved_volumes)
    info["calibration_volume"] = ANCHOR_SONOS_VOLUME

    # BT-settle. The 50 ms ramp completes long before this; the rest
    # of the wait is for the BT speakers' internal codec/DSP buffer to
    # drain so the mic doesn't pick up a fading echo of music that
    # virtual_out emitted seconds ago.
    info["bt_settle_sec"] = BT_SETTLE_SEC
    time.sleep(BT_SETTLE_SEC)

    # Use a synthetic "anchor" mac for filename uniqueness.
    capture = None
    try:
        capture = _capture_pair(
            capture_duration_sec, CAPTURE_DIR, "wifi-anchor",
            play_tune_wav=tune_path,
        )
    finally:
        for sock in bt_socks:
            _send_filter_command(sock, f"mute_to 1000 {RAMP_MS}")
        _restore_sonos_volumes(saved_volumes)

    if capture is None:
        info.update({"phase": "anchor_failed", "reason": "capture_failed"})
        on_event("anchor_failed", dict(info))
        return None, info

    try:
        from measurement.analyze_lag import estimate_lag_samples, load_wav_mono
    except ImportError as exc:
        info.update({"phase": "anchor_failed", "reason": "analyzer_import_failed",
                     "error": str(exc)})
        on_event("anchor_failed", dict(info))
        return None, info

    try:
        ref_arr, ref_sr = load_wav_mono(capture["ref"])
        mic_arr, mic_sr = load_wav_mono(capture["mic"])
    except Exception as exc:  # noqa: BLE001
        info.update({"phase": "anchor_failed", "reason": "wav_load_failed",
                     "error": repr(exc)})
        on_event("anchor_failed", dict(info))
        return None, info

    if ref_sr != mic_sr:
        info.update({"phase": "anchor_failed", "reason": "sample_rate_mismatch",
                     "ref_sr": ref_sr, "mic_sr": mic_sr})
        on_event("anchor_failed", dict(info))
        return None, info

    if mode == ANCHOR_MODE_MUSIC:
        import numpy as _np
        ref_rms = float(_np.sqrt(_np.mean(ref_arr ** 2))) if ref_arr.size else 0.0
        info["ref_rms"] = round(ref_rms, 2)
        if ref_rms < MIN_REF_RMS_MUSIC:
            info.update({
                "phase": "anchor_failed",
                "reason": "no_audio_playing",
                "ref_rms": round(ref_rms, 2),
                "ref_rms_floor": MIN_REF_RMS_MUSIC,
            })
            on_event("anchor_failed", dict(info))
            return None, info

    try:
        est = estimate_lag_samples(
            ref_arr, mic_arr, sample_rate=ref_sr,
            min_lag_ms=ANCHOR_MIN_LAG_MS, max_lag_ms=ANCHOR_MAX_LAG_MS,
        )
    except Exception as exc:  # noqa: BLE001
        info.update({"phase": "anchor_failed", "reason": "analyzer_failed",
                     "error": repr(exc)})
        on_event("anchor_failed", dict(info))
        return None, info

    measurement = {
        "lag_ms": round(est.lag_ms, 2),
        "confidence_primary": round(est.confidence_primary, 2),
        "confidence_secondary": round(est.confidence_secondary, 2),
        "sample_rate": est.sample_rate,
    }

    if mode == ANCHOR_MODE_CHIRP:
        min_p = ANCHOR_MIN_CONFIDENCE_PRIMARY_CHIRP
        min_s = ANCHOR_MIN_CONFIDENCE_SECONDARY_CHIRP
    else:
        min_p = ANCHOR_MIN_CONFIDENCE_PRIMARY_MUSIC
        min_s = ANCHOR_MIN_CONFIDENCE_SECONDARY_MUSIC

    if est.confidence_primary < min_p:
        info.update({"phase": "anchor_failed", "reason": "anchor_low_primary",
                     "measurement": measurement,
                     "threshold_primary": min_p})
        on_event("anchor_failed", dict(info))
        return None, info
    if est.confidence_secondary < min_s:
        info.update({"phase": "anchor_failed", "reason": "anchor_low_secondary",
                     "measurement": measurement,
                     "threshold_secondary": min_s})
        on_event("anchor_failed", dict(info))
        return None, info
    if not (ANCHOR_MIN_LAG_MS <= est.lag_ms <= ANCHOR_MAX_LAG_MS):
        info.update({"phase": "anchor_failed", "reason": "anchor_out_of_window",
                     "measurement": measurement})
        on_event("anchor_failed", dict(info))
        return None, info

    info.update({"phase": "anchor_measured", "anchor_lag_ms": round(est.lag_ms, 2),
                 "measurement": measurement})
    on_event("anchor_measured", dict(info))
    return float(est.lag_ms), info
