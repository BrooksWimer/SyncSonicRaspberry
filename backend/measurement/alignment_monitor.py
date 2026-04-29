"""Slice 4.4: continuous alignment-quality monitor (observation-only).

A daemon thread that runs every ALIGNMENT_PERIOD_SEC, captures
``virtual_out.monitor`` and the Jieli mic concurrently for a few
seconds WITH ALL SPEAKERS PLAYING, runs the Slice 4.1 analyzer, and
emits an ``alignment_quality_sample`` telemetry event. This commit
does NOT take any corrective action - the metric is observed and
logged so subsequent slices can build a data-informed threshold for
when to trigger a re-calibration cycle.

Why observation-only first
--------------------------
Audio-path correctness is paramount. We just landed a Slice 4.2
calibration that the user described as "incredible" and want to
preserve that. Adding a closed-loop runtime correction layer
introduces nonzero risk of misbehaviour on a system the user is
actively listening to. Landing the observation skeleton first lets
us:

- Verify the per-cycle CPU + capture cost is sustainable on the Pi
  (each cycle is one 4-second concurrent parecord pair plus an
  FFT-based cross-correlation, runs at ~1 / 60 sec rate).
- Capture a baseline of "natural" peak FWHM and lag over a long
  listening session so the Slice 4.5 threshold can be tuned with
  data instead of guesswork.
- Verify the metric's correlation with audible alignment quality:
  the user's ear remains the final authority and the FWHM/lag
  series should match their reports of "things sounded great" vs
  "things sounded smeared".

The peak-FWHM insight
---------------------
Cross-correlating the engine output (``virtual_out.monitor``) against
the mic capture during normal multi-speaker playback gives a
correlation curve whose peak is:
- NARROW (~5-15 ms FWHM) when all speakers arrive at the mic at
  approximately the same time - the mic captures one coherent sum,
  cross-correlation has one tight peak.
- BROAD or MULTI-PEAK (~30-100+ ms FWHM) when speakers are
  misaligned - the mic captures multiple shifted copies of the same
  music, cross-correlation has a smeared lobe with several
  competing local maxima.

This single number tracks whole-system alignment health WITHOUT
needing per-speaker source separation (which is hard) or per-speaker
isolation muting (which the user would notice during runtime).

Per-speaker attribution is deferred to Slice 4.5 / 4.6: if FWHM
degrades beyond threshold, trigger a re-calibration cycle that
either uses the brief-mute strategy from 4.2 or some smarter
band-separation trick. For now we just measure.

Process model
-------------
- Spawned by ``syncsonic_ble.main`` at service startup as a daemon
  thread. Failure inside the monitor must never break the audio
  service.
- One captures-and-analyzes cycle per ALIGNMENT_PERIOD_SEC. The
  capture itself takes CAPTURE_DURATION_SEC; analysis is sub-second
  on a Pi 4 for 4 sec of 48 kHz audio.
- Cycle is skipped (with a logged event) if any precondition fails:
  no speakers connected, virtual_out is suspended (no music
  playing), mic source missing, capture too quiet to analyze.
- WAV files are written to a tmpfs scratch dir and unlinked after
  analysis to avoid SD card wear and disk growth.
"""

from __future__ import annotations

import json
import os
import socket as _socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from syncsonic_ble.telemetry import EventType
from syncsonic_ble.telemetry.event_writer import emit
from syncsonic_ble.utils.logging_conf import get_logger

logger = get_logger(__name__)

SCRATCH_DIR = Path("/run/syncsonic/slice4_4_alignment")
SOCK_DIR = Path("/tmp/syncsonic-engine")
ALIGNMENT_PERIOD_SEC = 60.0
CAPTURE_DURATION_SEC = 4.0
MIN_SPEAKERS_FOR_MEASUREMENT = 2
# Below this RMS the capture is treated as silence; emitting an
# alignment metric on silence is meaningless and would pollute the
# trend data the Slice 4.5 threshold is going to be tuned against.
MIN_REF_RMS_DBFS = -55.0
MIN_MIC_RMS_DBFS = -60.0


def _resolve_mic_source() -> Optional[str]:
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and "alsa_input.usb-Jieli" in parts[1]:
            return parts[1]
    return None


def _virtual_out_state() -> Optional[str]:
    """Return 'RUNNING' / 'SUSPENDED' / etc for virtual_out, or None
    if pactl can't see it. Used to skip the cycle when nothing is
    playing - cross-correlating silence against silence yields
    a meaningless lag and would pollute the FWHM trend."""
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] == "virtual_out":
            return parts[-1]
    return None


def _count_active_filters() -> int:
    """Count live ``pw_delay_filter`` Unix sockets. Each socket
    corresponds to one connected speaker actively in the route table.
    Used as the gate for 'do we have enough speakers to make
    cross-correlation meaningful'."""
    if not SOCK_DIR.exists():
        return 0
    return len(list(SOCK_DIR.glob("syncsonic-delay-*.sock")))


def _capture_pair(duration_sec: float) -> Optional[Dict[str, Path]]:
    mic_source = _resolve_mic_source()
    if not mic_source:
        return None

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    ref_wav = SCRATCH_DIR / f"align_ref_{ts}.wav"
    mic_wav = SCRATCH_DIR / f"align_mic_{ts}.wav"

    common = [
        "parecord", "--file-format=wav", "--rate=48000",
        "--format=s16le", "--process-time-msec=20",
    ]
    ref_proc = subprocess.Popen(
        common + ["--device=virtual_out.monitor", "--channels=2", str(ref_wav)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    mic_proc = subprocess.Popen(
        common + [f"--device={mic_source}", "--channels=1", str(mic_wav)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        time.sleep(duration_sec)
    finally:
        for p in (ref_proc, mic_proc):
            try:
                p.send_signal(2)
                p.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                p.kill()
            except OSError:
                pass

    if not ref_wav.exists() or not mic_wav.exists():
        return None
    if ref_wav.stat().st_size < 1024 or mic_wav.stat().st_size < 1024:
        return None
    return {"ref": ref_wav, "mic": mic_wav}


def _signal_rms_dbfs(arr) -> float:
    """RMS in dBFS for a normalised float signal. ``arr`` is loaded
    via ``analyze_lag.load_wav_mono`` which returns int-range floats;
    we normalise by 32768 to get dBFS."""
    import math
    norm = arr / 32768.0
    rms = float((norm * norm).mean()) ** 0.5
    if rms < 1e-12:
        return -200.0
    return 20.0 * math.log10(rms)


class AlignmentMonitor:
    """Long-running alignment-quality observer.

    Use ``AlignmentMonitor().start()`` to spawn the daemon thread.
    The constructor's defaults match production cadence; tests can
    override ``period_sec`` for a faster run.
    """

    def __init__(
        self,
        period_sec: float = ALIGNMENT_PERIOD_SEC,
        capture_duration_sec: float = CAPTURE_DURATION_SEC,
    ) -> None:
        self._period_sec = float(period_sec)
        self._capture_duration_sec = float(capture_duration_sec)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cycle_count = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="syncsonic-alignment-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "AlignmentMonitor started (period=%.1fs, capture=%.1fs, observation-only)",
            self._period_sec, self._capture_duration_sec,
        )

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._thread = None
        logger.info("AlignmentMonitor stopped")

    def _run(self) -> None:
        # Wait one full period before first cycle, so the audio runtime
        # has fully come up and any speakers the user is going to
        # connect have had time to do so.
        self._stop.wait(self._period_sec)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception("AlignmentMonitor tick failed: %s", exc)
                emit(EventType.ALIGNMENT_QUALITY_SAMPLE, {
                    "phase": "tick_exception",
                    "error": repr(exc),
                })
            self._cycle_count += 1
            self._stop.wait(self._period_sec)

    def _tick(self) -> None:
        start_mono_ns = time.monotonic_ns()

        # Precondition gates. Each is a documented reason this cycle
        # cannot produce a meaningful measurement; emit a "skipped"
        # event so the analyzer can see how often we abstain and why.
        n_speakers = _count_active_filters()
        if n_speakers < MIN_SPEAKERS_FOR_MEASUREMENT:
            emit(EventType.ALIGNMENT_QUALITY_SAMPLE, {
                "phase": "skipped",
                "reason": "insufficient_speakers",
                "n_speakers": n_speakers,
                "cycle": self._cycle_count,
            })
            return

        vstate = _virtual_out_state()
        if vstate != "RUNNING":
            emit(EventType.ALIGNMENT_QUALITY_SAMPLE, {
                "phase": "skipped",
                "reason": "virtual_out_not_running",
                "virtual_out_state": vstate,
                "cycle": self._cycle_count,
            })
            return

        capture = _capture_pair(self._capture_duration_sec)
        if capture is None:
            emit(EventType.ALIGNMENT_QUALITY_SAMPLE, {
                "phase": "skipped",
                "reason": "capture_failed",
                "cycle": self._cycle_count,
            })
            return

        try:
            from measurement.analyze_lag import (  # type: ignore
                estimate_lag_samples, load_wav_mono,
            )
        except ImportError as exc:
            logger.warning("AlignmentMonitor analyzer import failed: %s", exc)
            self._cleanup_capture(capture)
            return

        try:
            ref_arr, ref_sr = load_wav_mono(capture["ref"])
            mic_arr, mic_sr = load_wav_mono(capture["mic"])
        except Exception as exc:  # noqa: BLE001
            emit(EventType.ALIGNMENT_QUALITY_SAMPLE, {
                "phase": "skipped",
                "reason": "wav_load_failed",
                "error": repr(exc),
                "cycle": self._cycle_count,
            })
            self._cleanup_capture(capture)
            return

        if ref_sr != mic_sr:
            emit(EventType.ALIGNMENT_QUALITY_SAMPLE, {
                "phase": "skipped",
                "reason": "sample_rate_mismatch",
                "ref_sr": ref_sr, "mic_sr": mic_sr,
                "cycle": self._cycle_count,
            })
            self._cleanup_capture(capture)
            return

        ref_rms_dbfs = _signal_rms_dbfs(ref_arr)
        mic_rms_dbfs = _signal_rms_dbfs(mic_arr)
        if ref_rms_dbfs < MIN_REF_RMS_DBFS or mic_rms_dbfs < MIN_MIC_RMS_DBFS:
            emit(EventType.ALIGNMENT_QUALITY_SAMPLE, {
                "phase": "skipped",
                "reason": "signal_too_quiet",
                "ref_rms_dbfs": round(ref_rms_dbfs, 1),
                "mic_rms_dbfs": round(mic_rms_dbfs, 1),
                "min_ref_rms_dbfs": MIN_REF_RMS_DBFS,
                "min_mic_rms_dbfs": MIN_MIC_RMS_DBFS,
                "cycle": self._cycle_count,
            })
            self._cleanup_capture(capture)
            return

        try:
            est = estimate_lag_samples(
                ref_arr, mic_arr, sample_rate=ref_sr,
                min_lag_ms=-50.0, max_lag_ms=700.0,
            )
        except Exception as exc:  # noqa: BLE001
            emit(EventType.ALIGNMENT_QUALITY_SAMPLE, {
                "phase": "skipped",
                "reason": "analyzer_failed",
                "error": repr(exc),
                "cycle": self._cycle_count,
            })
            self._cleanup_capture(capture)
            return

        elapsed_ms = (time.monotonic_ns() - start_mono_ns) / 1_000_000.0

        emit(EventType.ALIGNMENT_QUALITY_SAMPLE, {
            "phase": "measured",
            "cycle": self._cycle_count,
            "n_speakers": n_speakers,
            "lag_ms": round(est.lag_ms, 2),
            "peak_correlation": round(est.peak_correlation, 4),
            "confidence_primary": round(est.confidence_primary, 2),
            "confidence_secondary": round(est.confidence_secondary, 2),
            "peak_fwhm_samples": est.peak_fwhm_samples,
            "peak_fwhm_ms": round(est.peak_fwhm_ms, 2),
            "ref_rms_dbfs": round(ref_rms_dbfs, 1),
            "mic_rms_dbfs": round(mic_rms_dbfs, 1),
            "capture_duration_sec": self._capture_duration_sec,
            "elapsed_ms": round(elapsed_ms, 1),
        })
        self._cleanup_capture(capture)

    @staticmethod
    def _cleanup_capture(capture: Dict[str, Path]) -> None:
        # tmpfs is RAM-backed but bounded; unlink after analysis so we
        # don't accumulate ~400 KB per cycle indefinitely.
        for p in capture.values():
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


_MONITOR: Optional[AlignmentMonitor] = None


def get_alignment_monitor() -> Optional[AlignmentMonitor]:
    return _MONITOR


def build_and_start_alignment_monitor() -> AlignmentMonitor:
    """Process-wide singleton, started once at service init by main.py."""
    global _MONITOR
    if _MONITOR is None:
        _MONITOR = AlignmentMonitor()
    _MONITOR.start()
    return _MONITOR
