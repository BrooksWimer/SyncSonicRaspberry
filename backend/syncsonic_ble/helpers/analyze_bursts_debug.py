#!/usr/bin/env python3
"""
Standalone script to find 19 kHz burst ONSETS in a recording using simple,
data-driven processing. Run on saved debug WAVs to validate the algorithm
before porting to ultrasonic_sync.py.

Strategy (no time-gap heuristics):
  1. Bandpass filter around 19 kHz → isolate burst energy.
  2. Short-time energy (e.g. RMS in small windows) → 1D "19 kHz level vs time".
  3. Threshold that energy (e.g. fraction of max, or percentile) → binary "in burst".
  4. Find contiguous above-threshold segments; each segment = one burst.
  5. t1, t2 = start time of first two segments (onset = leading edge of each burst).

Usage:
  python analyze_bursts_debug.py [path/to/recording.wav]
  Default path: syncsonic_debug/last_recording.wav (relative to cwd or repo root).

Output:
  - Prints t1, t2 (seconds) and spacing.
  - Saves spectrogram with vertical lines at detected onsets to
    <same_dir_as_wav>/spectrogram_analyzed.png
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import wave

import numpy as np
from scipy import signal as scipy_signal

# Match recording params (same as ultrasonic_sync)
SAMPLE_RATE = 48000
BURST_FREQ_HZ = 19000
# Bandpass: narrow around 19 kHz
BANDPASS_LOW_HZ = 18000
BANDPASS_HIGH_HZ = 20000
# Short-time energy window (ms)
ENERGY_WINDOW_MS = 10
# Minimum burst duration (ms) — ignore tiny blips
MIN_BURST_DURATION_MS = 50
# Merge segments closer than this (ms) into one burst
MIN_GAP_BETWEEN_BURSTS_MS = 100


def load_wav_mono(path: str) -> tuple[np.ndarray, int]:
    """Load WAV as float mono, return (samples, sample_rate)."""
    with wave.open(path, "rb") as wav:
        sr = wav.getframerate()
        nch = wav.getnchannels()
        n = wav.getnframes()
        raw = wav.readframes(n)
    if nch == 2:
        # stereo: take left channel
        vals = np.frombuffer(raw, dtype=np.int16)
        vals = vals[::2].astype(np.float64) / 32768.0
    else:
        vals = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    return vals, sr


def bandpass(x: np.ndarray, sr: float, low_hz: float, high_hz: float, order: int = 4) -> np.ndarray:
    """Zero-phase bandpass filter (forward-backward)."""
    nyq = sr / 2.0
    low = max(0.01, low_hz / nyq)
    high = min(0.99, high_hz / nyq)
    b, a = scipy_signal.butter(order, [low, high], btype="band")
    return scipy_signal.filtfilt(b, a, x)


def short_time_energy(
    x: np.ndarray, window_samples: int, hop: int, sr: float
) -> tuple[np.ndarray, np.ndarray]:
    """RMS energy in sliding windows. Returns (time_sec, energy)."""
    n = len(x)
    num_win = max(0, (n - window_samples) // hop + 1)
    t_sec = (np.arange(num_win) * hop + window_samples / 2) / sr
    energy = np.zeros(num_win)
    for i in range(num_win):
        start = i * hop
        end = start + window_samples
        if end > n:
            break
        energy[i] = np.sqrt(np.mean(x[start:end] ** 2))
    return t_sec[: len(energy)], energy


def find_contiguous_segments(
    t_sec: np.ndarray,
    above: np.ndarray,
    min_duration_sec: float,
    max_gap_sec: float,
) -> list[tuple[float, float]]:
    """
    Find contiguous segments where above is True.
    Merge segments that are closer than max_gap_sec.
    Return list of (start_sec, end_sec) for segments lasting at least min_duration_sec.
    """
    if len(t_sec) == 0 or len(above) == 0:
        return []
    dt = t_sec[1] - t_sec[0] if len(t_sec) > 1 else 0.01
    segments: list[tuple[float, float]] = []
    in_segment = False
    seg_start = 0.0
    for i in range(len(above)):
        if above[i] and not in_segment:
            seg_start = t_sec[i]
            in_segment = True
        elif not above[i] and in_segment:
            seg_end = t_sec[i - 1]
            in_segment = False
            if seg_end - seg_start >= min_duration_sec:
                # Merge with previous segment if gap is small
                if segments and (seg_start - segments[-1][1]) <= max_gap_sec:
                    segments[-1] = (segments[-1][0], seg_end)
                else:
                    segments.append((seg_start, seg_end))
    if in_segment and (t_sec[-1] - seg_start) >= min_duration_sec:
        if segments and (seg_start - segments[-1][1]) <= max_gap_sec:
            segments[-1] = (segments[-1][0], t_sec[-1])
        else:
            segments.append((seg_start, t_sec[-1]))
    return segments


def detect_burst_onsets(
    wav_path: str,
    sr: int = SAMPLE_RATE,
    band_low: float = BANDPASS_LOW_HZ,
    band_high: float = BANDPASS_HIGH_HZ,
    energy_window_ms: float = ENERGY_WINDOW_MS,
    threshold_frac: float = 0.35,
    min_burst_duration_ms: float = MIN_BURST_DURATION_MS,
    max_gap_ms: float = MIN_GAP_BETWEEN_BURSTS_MS,
    debug: bool = False,
) -> tuple[float | None, float | None, list[tuple[float, float]] | None]:
    """
    Data-driven burst onset detection.
    Returns (t1_sec, t2_sec) for the start of the first two 19 kHz burst segments,
    or (None, None) if fewer than two segments.
    """
    x, file_sr = load_wav_mono(wav_path)
    if file_sr != sr:
        # Resample to sr if needed (simple repeat/decimate for same rate only for now)
        print(f"Warning: file sr={file_sr}, expected {sr}; using file sr.", file=sys.stderr)
        sr = file_sr

    # 1) Bandpass around 19 kHz
    filtered = bandpass(x, sr, band_low, band_high)

    # 2) Short-time energy (RMS in small windows)
    win_samples = max(64, int(sr * energy_window_ms / 1000))
    hop = max(1, win_samples // 2)
    t_sec, energy = short_time_energy(filtered, win_samples, hop, sr)
    if len(t_sec) == 0:
        return None, None

    # 3) Threshold: burst = energy above fraction of max (bursts are the dominant 19 kHz events)
    emax = float(np.max(energy))
    threshold = emax * threshold_frac
    threshold = max(threshold, 1e-9)
    above = energy >= threshold

    # 4) Contiguous segments = bursts; take start of first two
    min_dur_sec = min_burst_duration_ms / 1000.0
    max_gap_sec = max_gap_ms / 1000.0
    segments = find_contiguous_segments(t_sec, above, min_dur_sec, max_gap_sec)

    if debug:
        # Save energy vs time and threshold for inspection
        debug_dir = os.path.dirname(wav_path)
        if debug_dir:
            np.savetxt(
                os.path.join(debug_dir, "analyze_energy_debug.csv"),
                np.column_stack([t_sec, energy, np.full_like(t_sec, threshold)]),
                delimiter=",",
                header="time_sec,energy,threshold",
                comments="",
            )
        segs_str = [(float(a), float(b)) for a, b in segments[:5]]
        print(f"Debug: threshold={threshold:.2e} (emax={emax:.2e}) segments={len(segments)} {segs_str}", file=sys.stderr)

    if len(segments) < 2:
        return None, None, segments if debug else None
    # Use segment start as onset; optionally refine to "first rise" (earlier crossing)
    t1 = _refine_onset(t_sec, energy, segments[0], frac=0.2)
    t2 = _refine_onset(t_sec, energy, segments[1], frac=0.2)
    return t1, t2, (segments if debug else None)


def _refine_onset(
    t_sec: np.ndarray,
    energy: np.ndarray,
    segment: tuple[float, float],
    frac: float = 0.2,
) -> float:
    """Within segment, find first time energy crosses above (min + frac*(max-min)) from below."""
    seg_start, seg_end = segment
    mask = (t_sec >= seg_start - 0.1) & (t_sec <= seg_end + 0.1)
    t = t_sec[mask]
    e = energy[mask]
    if len(e) == 0:
        return seg_start
    lo = float(np.min(e))
    hi = float(np.max(e))
    thresh = lo + frac * (hi - lo)
    for i in range(len(e)):
        if e[i] >= thresh:
            return float(t[i])
    return seg_start


def main() -> None:
    ap = argparse.ArgumentParser(description="Detect 19 kHz burst onsets in a recording (debug).")
    ap.add_argument(
        "wav",
        nargs="?",
        default=os.path.join(os.getcwd(), "syncsonic_debug", "last_recording.wav"),
        help="Path to WAV (default: syncsonic_debug/last_recording.wav)",
    )
    ap.add_argument(
        "-o", "--output",
        default=None,
        help="Output spectrogram PNG path (default: same dir as WAV, spectrogram_analyzed.png)",
    )
    ap.add_argument("-t", "--threshold", type=float, default=0.35, help="Threshold as fraction of max 19 kHz energy (default 0.35)")
    ap.add_argument("--debug", action="store_true", help="Print segment count and save energy curve to analyze_energy_debug.csv")
    args = ap.parse_args()

    wav_path = os.path.abspath(args.wav)
    if not os.path.isfile(wav_path):
        # Try from repo root
        alt = os.path.join(os.path.dirname(__file__), "..", "..", "..", "syncsonic_debug", "last_recording.wav")
        alt = os.path.normpath(alt)
        if os.path.isfile(alt):
            wav_path = alt
        else:
            print(f"Error: not found: {args.wav}", file=sys.stderr)
            sys.exit(1)

    t1, t2, _ = detect_burst_onsets(
        wav_path,
        threshold_frac=args.threshold,
        debug=args.debug,
    )
    if t1 is None:
        print("Could not detect two burst segments. Try -t 0.25 or --debug to inspect.")
        sys.exit(1)

    print(f"t1={t1:.4f} s  t2={t2:.4f} s  spacing={t2 - t1:.4f} s")

    # Spectrogram with markers
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping spectrogram.", file=sys.stderr)
        sys.exit(0)

    x, sr = load_wav_mono(wav_path)
    nperseg = min(2048, len(x) // 4)
    if nperseg < 64:
        print("Recording too short for spectrogram.", file=sys.stderr)
        sys.exit(0)
    f, t_spec, Sxx = scipy_signal.spectrogram(x, sr, nperseg=nperseg)
    Sxx_db = 10 * np.log10(Sxx + 1e-12)

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.pcolormesh(t_spec, f, Sxx_db, shading="auto", cmap="viridis")
    ax.axvline(x=t1, color="cyan", linewidth=1.5, label=f"t1 (first onset) {t1:.3f}s")
    ax.axvline(x=t2, color="orange", linewidth=1.5, label=f"t2 (second onset) {t2:.3f}s")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Frequency (Hz)")
    ax.set_title("Recording spectrogram — analyzed burst onsets")
    ax.legend(loc="upper right")
    ax.set_ylim(0, min(24000, f.max()))

    out_path = args.output
    if out_path is None:
        out_path = os.path.join(os.path.dirname(wav_path), "spectrogram_analyzed.png")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
