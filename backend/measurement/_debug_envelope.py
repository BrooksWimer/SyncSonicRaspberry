"""Debug helper: print energy-envelope timestamps of ref and mic.

Computes a smoothed |signal| envelope, then reports where the energy
crosses 50 % of its peak. The difference between mic onset and ref
onset is the acoustic lag — a primitive but extremely robust
estimator that is immune to MP3-induced phase distortion.
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        nchan = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if nchan > 1:
        arr = arr.reshape(-1, nchan).mean(axis=1)
    return arr, sr


def smooth_envelope(x: np.ndarray, sr: int, window_ms: float = 30.0) -> np.ndarray:
    """Smoothed |signal| envelope using a moving-average rectangular window."""
    win = max(1, int(round(window_ms * sr / 1000.0)))
    rect = np.abs(x)
    kernel = np.ones(win, dtype=np.float64) / win
    return np.convolve(rect, kernel, mode="same")


def find_onset(env: np.ndarray, sr: int, *, threshold_frac: float = 0.5,
               min_dwell_ms: float = 50.0) -> float | None:
    """Return time (sec) where envelope rises above threshold_frac * peak
    and stays above for at least min_dwell_ms.
    """
    peak = float(env.max())
    if peak <= 0:
        return None
    th = peak * threshold_frac
    above = env >= th
    dwell = int(round(min_dwell_ms * sr / 1000.0))
    # find first index where above[i:i+dwell] is all True
    for i in range(len(above) - dwell):
        if above[i] and above[i:i + dwell].all():
            return i / sr
    return None


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: _debug_envelope.py <ref.wav> <mic.wav>")
        return 1
    ref_p, mic_p = Path(sys.argv[1]), Path(sys.argv[2])
    ref, ref_sr = load_wav(ref_p)
    mic, mic_sr = load_wav(mic_p)
    if ref_sr != mic_sr:
        print(f"sample rate mismatch ref={ref_sr} mic={mic_sr}")
        return 2
    sr = ref_sr

    ref_env = smooth_envelope(ref, sr, 30.0)
    mic_env = smooth_envelope(mic, sr, 30.0)

    print(f"ref samples={len(ref)}  peak={ref_env.max():.4f}  median={np.median(ref_env):.6f}")
    print(f"mic samples={len(mic)}  peak={mic_env.max():.4f}  median={np.median(mic_env):.6f}")

    # Print envelope amplitude per 100ms bucket so we can see when the chirp arrives.
    bucket_ms = 100
    n_buckets = int(len(mic) / sr * 1000 / bucket_ms)
    print("\nEnergy buckets (peak in 100 ms windows):")
    print(f"{'time':>7s}  {'ref_peak':>10s}  {'mic_peak':>10s}")
    for b in range(n_buckets):
        i0 = int(b * bucket_ms * sr / 1000)
        i1 = int((b + 1) * bucket_ms * sr / 1000)
        rp = float(ref_env[i0:i1].max()) if i1 <= len(ref_env) else 0.0
        mp = float(mic_env[i0:i1].max()) if i1 <= len(mic_env) else 0.0
        bar_r = "#" * int(rp * 100)
        bar_m = "*" * int(mp * 1000)  # mic is 10x quieter
        print(f"{b * bucket_ms / 1000:6.1f}s  {rp:10.4f}  {mp:10.4f}  {bar_r:30s} {bar_m}")

    # Onset detection
    for th in (0.3, 0.5, 0.7):
        ref_onset = find_onset(ref_env, sr, threshold_frac=th)
        mic_onset = find_onset(mic_env, sr, threshold_frac=th)
        ref_s = f"{ref_onset:.3f}" if ref_onset is not None else "n/a"
        mic_s = f"{mic_onset:.3f}" if mic_onset is not None else "n/a"
        if ref_onset is not None and mic_onset is not None:
            lag_ms = (mic_onset - ref_onset) * 1000
            print(f"\nthreshold={th:.0%}  ref_onset={ref_s}s  mic_onset={mic_s}s  lag={lag_ms:.1f} ms")
        else:
            print(f"\nthreshold={th:.0%}  ref_onset={ref_s}s  mic_onset={mic_s}s  lag=n/a")
    return 0


if __name__ == "__main__":
    sys.exit(main())
