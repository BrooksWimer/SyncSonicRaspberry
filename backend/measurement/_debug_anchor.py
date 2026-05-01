"""Debug helper: dump multiple-peak structure of an anchor capture.

Reads ref+mic pair, computes cross-correlation across [-50, 3500] ms,
prints the top 5 peaks with their lags and amplitudes. Used to figure
out why the anchor picked an obviously wrong peak.
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
from scipy import signal as ss


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        nchan = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    if sw == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    else:
        raise SystemExit(f"unsupported sw={sw}")
    if nchan > 1:
        arr = arr.reshape(-1, nchan).mean(axis=1)
    return arr, sr


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: _debug_anchor.py <ref.wav> <mic.wav>")
        return 1
    ref_p, mic_p = Path(sys.argv[1]), Path(sys.argv[2])
    ref, ref_sr = load_wav(ref_p)
    mic, mic_sr = load_wav(mic_p)
    if ref_sr != mic_sr:
        print(f"sample rate mismatch ref={ref_sr} mic={mic_sr}")
        return 2
    sr = ref_sr
    print(f"ref samples={len(ref)} dur={len(ref)/sr:.3f}s | mic samples={len(mic)} dur={len(mic)/sr:.3f}s | sr={sr}")
    print(f"ref rms={np.sqrt(np.mean(ref**2)):.4f} peak={np.max(np.abs(ref)):.4f}")
    print(f"mic rms={np.sqrt(np.mean(mic**2)):.4f} peak={np.max(np.abs(mic)):.4f}")

    n = max(len(ref), len(mic))
    ref_p2 = np.pad(ref, (0, n - len(ref)))
    mic_p2 = np.pad(mic, (0, n - len(mic)))

    corr = ss.fftconvolve(mic_p2, ref_p2[::-1], mode="full")
    zero_idx = len(ref_p2) - 1

    min_lag_ms, max_lag_ms = -50.0, 3500.0
    min_idx = zero_idx + int(min_lag_ms * sr / 1000.0)
    max_idx = zero_idx + int(max_lag_ms * sr / 1000.0)
    win = corr[min_idx : max_idx + 1].copy()
    norm = np.abs(win)
    print(f"\nsearch window: lag in [{min_lag_ms}, {max_lag_ms}] ms  -> {len(win)} samples")
    print(f"window stats: max={norm.max():.4f} mean={norm.mean():.6f} std={norm.std():.6f}")

    print("\nTop 10 peaks (with 100 ms guard between picks):")
    guard_samples = int(0.100 * sr)
    norm_work = norm.copy()
    for i in range(10):
        idx = int(np.argmax(norm_work))
        amp = norm_work[idx]
        lag_ms = (idx - (zero_idx - min_idx)) * 1000.0 / sr  # idx is offset from min_lag
        lag_ms_abs = min_lag_ms + idx * 1000.0 / sr
        ratio_to_mean = amp / max(norm.mean(), 1e-12)
        print(f"  #{i+1}: lag={lag_ms_abs:7.2f} ms  amp={amp:.4f}  amp/mean={ratio_to_mean:5.2f}x")
        lo = max(0, idx - guard_samples)
        hi = min(len(norm_work), idx + guard_samples + 1)
        norm_work[lo:hi] = 0.0
    return 0


if __name__ == "__main__":
    sys.exit(main())
