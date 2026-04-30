"""Slice 4.1: offline lag estimation between a reference and a mic capture.

Given two audio signals captured during the same wall-clock window
(typically a virtual_out.monitor capture as the "what we sent" reference
and a mic capture as the "what was heard" measurement), this module
estimates the integer-sample lag between them using FFT-based normalised
cross-correlation. The lag is the fundamental input the Slice 4 mic-driven
alignment loop needs to push delay corrections through the Coordinator
back to the per-speaker C delay filters.

Why FFT-based, not time-domain
-------------------------------
A Pi 4 can comfortably correlate two 5-second 48 kHz signals via
``scipy.signal.correlate(method='fft')`` in well under 100 ms.
Time-domain ``np.correlate(mode='full')`` would be O(N^2) for the same
length, ~50 seconds. We size analysis windows for the fast path.

Why normalised, not raw, correlation
-------------------------------------
The mic signal is heavily attenuated by acoustic propagation (typical
speaker-to-listener path delivers -30 to -50 dB vs the reference electric
signal). Raw cross-correlation peaks scale with signal energy, so
loud-section vs quiet-section comparisons are unstable. Normalised
correlation (Pearson coefficient form) returns peak values in [-1, 1]
that are interpretable as similarity rather than energy-weighted match.

Why we deliberately do NOT do sub-sample lag estimation here
-------------------------------------------------------------
Sub-sample interpolation (parabolic peak fit etc) buys 5-10x finer
resolution but is only meaningful when the integer-sample peak is
already unambiguous. Slice 4.1 is the validation slice: we want to
prove the integer-sample answer is right before adding interpolation.
At 48 kHz, 1 sample = 20.8 us, well below human-perceptible inter-
speaker drift (~1 ms). Integer samples are sufficient for Slice 4.

Confidence
----------
We report ``confidence = peak / mean`` and ``confidence_secondary =
peak / second_peak`` so the caller can reject low-confidence results
(e.g. quiet music passages, mic in a noisy environment, no overlapping
content between reference and capture).
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Tuple

import numpy as np


@dataclass
class LagEstimate:
    """Result of one cross-correlation analysis.

    Sign convention: ``lag_samples > 0`` means the captured signal is
    DELAYED relative to the reference (i.e. captured[t] matches
    reference[t - lag]). This matches physical intuition - for a
    speaker the mic always captures the audio later than the moment
    it left the engine, so we expect positive lags.
    """
    lag_samples: int
    lag_ms: float
    peak_correlation: float       # in [-1, 1]
    confidence_primary: float     # peak / mean(|corr|); >5 is solid
    confidence_secondary: float   # peak / second-largest peak; >2 is unambiguous
    sample_rate: int
    reference_n_samples: int
    captured_n_samples: int
    search_window_samples: Tuple[int, int]  # (min_lag, max_lag) actually searched
    # Optional correlation peak width (FWHM of |corr| around argmax).
    # Useful diagnostics for isolated-speaker captures (Slice 4.2): a
    # narrow peak usually means an unambiguous lag; a very wide peak
    # can indicate bleed from another source or a diffuse impulse.
    peak_fwhm_samples: int = 0
    peak_fwhm_ms: float = 0.0

    def to_json(self) -> str:
        d = asdict(self)
        d["search_window_samples"] = list(d["search_window_samples"])
        return json.dumps(d, indent=2)


def _compute_peak_fwhm(window: "np.ndarray", peak_idx: int) -> int:
    """Return the Full-Width-at-Half-Maximum of the correlation peak,
    in samples. ``window`` is the signed correlation slice the
    estimator searched, ``peak_idx`` is the argmax of ``|window|``.

    We measure FWHM on the absolute correlation so anti-phase peaks
    are treated symmetrically with in-phase ones (cross-correlation
    sign depends on speaker phase response, but the peak's WIDTH is
    physically meaningful regardless of sign).

    A wide peak indicates the captured signal is a SUM of multiple
    delayed copies (i.e. the speakers are not aligned to each other);
    a narrow peak indicates one coherent arrival. Returning width in
    integer samples keeps it composable with the analyzer's
    integer-sample lag convention.
    """
    abs_window = np.abs(window)
    peak_val = float(abs_window[peak_idx])
    if peak_val <= 0.0:
        return 0
    half = peak_val * 0.5
    # Walk left and right from the peak until we drop below half-max.
    # If we never do (peak fills the whole search window), return the
    # window length as a saturated value - the analyzer caller can
    # interpret that as "FWHM at least this wide".
    left = peak_idx
    while left > 0 and abs_window[left] >= half:
        left -= 1
    right = peak_idx
    while right < len(abs_window) - 1 and abs_window[right] >= half:
        right += 1
    return int(right - left)


def _to_mono(signal: np.ndarray) -> np.ndarray:
    """Mix-down to mono. ``signal`` may be 1-D (already mono) or 2-D
    (samples, channels)."""
    if signal.ndim == 1:
        return signal
    if signal.ndim == 2:
        return signal.mean(axis=1)
    raise ValueError(f"unsupported signal shape: {signal.shape}")


def _normalize(signal: np.ndarray) -> np.ndarray:
    """Subtract DC offset and scale to unit RMS so cross-correlations
    are interpretable as Pearson similarity, not energy-weighted match.
    Returns float64 unconditionally."""
    s = signal.astype(np.float64, copy=False)
    s = s - s.mean()
    rms = float(np.sqrt(np.mean(s * s)))
    if rms < 1e-12:
        # Silent input - any correlation is meaningless. Return as-is;
        # the caller will see a near-zero peak and reject it.
        return s
    return s / rms


def estimate_lag_samples(
    reference: np.ndarray,
    captured: np.ndarray,
    sample_rate: int,
    max_lag_ms: float = 500.0,
    min_lag_ms: float = -50.0,
) -> LagEstimate:
    """Cross-correlate ``captured`` against ``reference`` and return
    the best integer-sample lag.

    Both inputs may be mono or stereo; stereo is mixed down to mono
    before correlation. Both are normalised to zero mean + unit RMS so
    the peak correlation is in [-1, 1] and is comparable across
    different absolute signal levels.

    The search window is constrained to ``[min_lag_ms, max_lag_ms]``
    (default -50 ms .. +500 ms). For the SyncSonic use case:
      - BlueZ buffer + audio path = +50 to +250 ms typical
      - Acoustic propagation 0-3 m = +0 to +9 ms additional
      - Negative lag should never be physically possible (we cannot
        capture audio before it's emitted), but we leave a small
        negative window so a misclock between reference and capture
        is observable rather than wrapping around to a giant positive.
    """
    if reference.size == 0 or captured.size == 0:
        raise ValueError("reference and captured must be non-empty")

    ref = _normalize(_to_mono(reference))
    cap = _normalize(_to_mono(captured))

    # Use scipy.signal.correlate for FFT-based fast convolution; this
    # is the only place we depend on scipy. ``method='fft'`` is the
    # default for sufficiently large inputs but we set it explicitly
    # to make the performance characteristic obvious.
    from scipy.signal import correlate

    # ``correlate(cap, ref, mode='full')`` returns an array of length
    # len(cap) + len(ref) - 1 where index i corresponds to lag
    # i - (len(ref) - 1). A positive lag means cap is delayed relative
    # to ref (cap[t] matches ref[t - lag]).
    full = correlate(cap, ref, mode="full", method="fft")
    zero_lag_idx = len(ref) - 1

    # Convert lag bounds to indices into ``full``.
    min_lag_samples = int(round(min_lag_ms * sample_rate / 1000.0))
    max_lag_samples = int(round(max_lag_ms * sample_rate / 1000.0))
    lo_idx = max(0, zero_lag_idx + min_lag_samples)
    hi_idx = min(len(full) - 1, zero_lag_idx + max_lag_samples)
    if lo_idx > hi_idx:
        raise ValueError(
            f"search window empty; min_lag_ms={min_lag_ms}, "
            f"max_lag_ms={max_lag_ms}, ref={len(ref)}, cap={len(cap)}"
        )

    window = full[lo_idx : hi_idx + 1]
    abs_window = np.abs(window)
    peak_idx_in_window = int(np.argmax(abs_window))
    peak_idx_in_full = lo_idx + peak_idx_in_window

    # Normalise the peak to [-1, 1]: divide by the geometric mean of
    # the two signals' energies. With both inputs unit-RMS and length
    # ``min(len(ref), len(cap))``, the perfect-overlap energy is the
    # overlap length itself, so dividing by that yields Pearson r at
    # the best lag.
    overlap = min(len(ref), len(cap))
    peak_corr_normalised = float(window[peak_idx_in_window]) / overlap

    # Confidence metrics. Use the mean of the searched window and the
    # second-largest local maximum (excluding a 50 ms guard around the
    # peak so we don't count adjacent samples on the same correlation
    # lobe). Cross-correlating music against itself produces a wide
    # central lobe (typically 10-30 ms wide); we want to compare the
    # peak against ECHOES or unrelated signal, not against the slope
    # of its own lobe.
    mean_abs = float(np.mean(abs_window))
    confidence_primary = float(np.abs(window[peak_idx_in_window]) / mean_abs) if mean_abs > 0 else 0.0
    guard_samples = max(1, int(round(0.050 * sample_rate)))  # 50 ms guard
    masked = abs_window.copy()
    lo_mask = max(0, peak_idx_in_window - guard_samples)
    hi_mask = min(len(masked), peak_idx_in_window + guard_samples + 1)
    masked[lo_mask:hi_mask] = 0.0
    second_peak = float(np.max(masked)) if masked.size else 0.0
    confidence_secondary = (
        float(np.abs(window[peak_idx_in_window]) / second_peak)
        if second_peak > 0 else float("inf")
    )

    lag_samples = peak_idx_in_full - zero_lag_idx
    lag_ms = lag_samples * 1000.0 / sample_rate

    # Slice 4.4: peak FWHM as alignment-health signal.
    fwhm_samples = _compute_peak_fwhm(window, peak_idx_in_window)
    fwhm_ms = fwhm_samples * 1000.0 / sample_rate

    return LagEstimate(
        lag_samples=int(lag_samples),
        lag_ms=float(lag_ms),
        peak_correlation=peak_corr_normalised,
        confidence_primary=confidence_primary,
        confidence_secondary=confidence_secondary,
        sample_rate=sample_rate,
        reference_n_samples=len(ref),
        captured_n_samples=len(cap),
        search_window_samples=(int(min_lag_samples), int(max_lag_samples)),
        peak_fwhm_samples=int(fwhm_samples),
        peak_fwhm_ms=float(fwhm_ms),
    )


def load_wav_mono(path: Path) -> Tuple[np.ndarray, int]:
    """Read a WAV file and return (signal_int16_or_float, sample_rate).
    Stereo or higher channel count is mixed to mono."""
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n_channels = w.getnchannels()
        n_frames = w.getnframes()
        sampwidth = w.getsampwidth()
        raw = w.readframes(n_frames)
    if sampwidth == 2:
        sig = np.frombuffer(raw, dtype=np.int16)
    elif sampwidth == 4:
        sig = np.frombuffer(raw, dtype=np.int32)
    elif sampwidth == 1:
        # Unsigned 8-bit; convert to signed range
        sig = np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128
    else:
        raise ValueError(f"unsupported sample width: {sampwidth} bytes")
    if n_channels > 1:
        sig = sig.reshape(-1, n_channels).mean(axis=1)
    return sig.astype(np.float64), sr


def _cli() -> int:
    p = argparse.ArgumentParser(
        prog="python -m measurement.analyze_lag",
        description=__doc__.split("\n\n", 1)[0],
    )
    p.add_argument("--ref", required=True, type=Path,
                   help="reference WAV (e.g. virtual_out.monitor capture)")
    p.add_argument("--captured", required=True, type=Path,
                   help="captured WAV (typically a mic recording)")
    p.add_argument("--max-lag-ms", type=float, default=500.0,
                   help="search window upper bound in ms (default 500)")
    p.add_argument("--min-lag-ms", type=float, default=-50.0,
                   help="search window lower bound in ms (default -50)")
    p.add_argument("--json", action="store_true",
                   help="print machine-readable JSON instead of human text")
    args = p.parse_args()

    ref, ref_sr = load_wav_mono(args.ref)
    cap, cap_sr = load_wav_mono(args.captured)
    if ref_sr != cap_sr:
        print(
            f"refusing: reference SR {ref_sr} != captured SR {cap_sr}; "
            "resample upstream so both are at the same rate",
            file=sys.stderr,
        )
        return 2

    est = estimate_lag_samples(
        ref, cap, sample_rate=ref_sr,
        max_lag_ms=args.max_lag_ms, min_lag_ms=args.min_lag_ms,
    )
    if args.json:
        print(est.to_json())
    else:
        print(f"reference: {args.ref.name}  ({est.reference_n_samples} samples @ {ref_sr} Hz)")
        print(f"captured:  {args.captured.name}  ({est.captured_n_samples} samples)")
        print(f"")
        print(f"lag:                  {est.lag_samples} samples = {est.lag_ms:+.2f} ms")
        print(f"peak correlation:     {est.peak_correlation:+.4f}  (Pearson r at best lag)")
        print(f"confidence primary:   {est.confidence_primary:.2f}x  (peak / mean,  >5 is solid)")
        print(f"confidence secondary: {est.confidence_secondary:.2f}x  (peak / 2nd-peak, >2 unambiguous)")
        print(f"peak FWHM:            {est.peak_fwhm_samples} samples = {est.peak_fwhm_ms:.1f} ms"
              f"  (narrow=aligned, wide=misaligned)")
        print(f"search window:        {est.search_window_samples[0]}..{est.search_window_samples[1]} samples"
              f"  ({est.search_window_samples[0]*1000.0/ref_sr:+.0f}..{est.search_window_samples[1]*1000.0/ref_sr:+.0f} ms)")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
