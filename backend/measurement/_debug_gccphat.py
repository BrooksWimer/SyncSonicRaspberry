"""Debug helper: run GCC-PHAT on an anchor capture to compare against direct
cross-correlation. PHAT (Phase Transform) whitens the spectrum so the
correlation peak collapses toward an impulse, which is far more robust
than the raw correlation when one signal has been MP3-encoded and
processed by a Sonos's audio chain.
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


def gcc_phat(ref: np.ndarray, mic: np.ndarray, sr: int) -> tuple[np.ndarray, int]:
    """Return (cc, zero_lag_idx). cc[zero_lag_idx + k] ~ correlation at +k samples
    (mic delayed by k samples relative to ref)."""
    n = len(ref) + len(mic)
    # Round up to next power of 2 for FFT speed
    pow2 = 1
    while pow2 < n:
        pow2 <<= 1
    REF = np.fft.rfft(ref, pow2)
    MIC = np.fft.rfft(mic, pow2)
    R = MIC * np.conj(REF)
    mag = np.abs(R)
    R = R / (mag + 1e-10)  # PHAT weighting
    cc = np.fft.irfft(R, pow2)
    zero_lag_idx = 0  # by IFFT convention; positive lags are cc[1..pow2/2]
    return cc, pow2


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: _debug_gccphat.py <ref.wav> <mic.wav>")
        return 1
    ref_p, mic_p = Path(sys.argv[1]), Path(sys.argv[2])
    ref, ref_sr = load_wav(ref_p)
    mic, mic_sr = load_wav(mic_p)
    if ref_sr != mic_sr:
        print(f"sample rate mismatch ref={ref_sr} mic={mic_sr}")
        return 2
    sr = ref_sr

    print(f"ref dur={len(ref)/sr:.3f}s rms={np.sqrt(np.mean(ref**2)):.4f}")
    print(f"mic dur={len(mic)/sr:.3f}s rms={np.sqrt(np.mean(mic**2)):.4f}")

    cc, fft_n = gcc_phat(ref, mic, sr)

    # Look at lags in [-50, 3500] ms; positive = mic delayed.
    min_lag_ms, max_lag_ms = -50.0, 3500.0
    min_idx = int(min_lag_ms * sr / 1000.0)
    max_idx = int(max_lag_ms * sr / 1000.0)
    # Build window. Negative lags wrap around to the end of cc.
    if min_idx < 0:
        neg_part = cc[min_idx:]  # last |min_idx| samples
        pos_part = cc[: max_idx + 1]
        window = np.concatenate([neg_part, pos_part])
        lag_offset_samples = min_idx  # window[0] corresponds to lag=min_idx samples
    else:
        window = cc[min_idx : max_idx + 1].copy()
        lag_offset_samples = min_idx

    norm = np.abs(window)
    print(f"\nGCC-PHAT window: {len(window)} samples  max={norm.max():.4f}  mean={norm.mean():.6f}  std={norm.std():.6f}")

    # Find top 10 peaks with 100ms guard
    print("\nTop 10 PHAT peaks (100 ms guard):")
    guard = int(0.100 * sr)
    work = norm.copy()
    for i in range(10):
        idx = int(np.argmax(work))
        amp = work[idx]
        lag_samples = lag_offset_samples + idx
        lag_ms = lag_samples * 1000.0 / sr
        ratio = amp / max(norm.mean(), 1e-12)
        print(f"  #{i+1}: lag={lag_ms:7.2f} ms  amp={amp:.4f}  amp/mean={ratio:6.2f}x")
        lo, hi = max(0, idx - guard), min(len(work), idx + guard + 1)
        work[lo:hi] = 0.0
    return 0


if __name__ == "__main__":
    sys.exit(main())
