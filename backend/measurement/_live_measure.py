"""Live passive measurement: capture mic + virtual_out.monitor for N seconds
without muting/interrupting anything, then cross-correlate to locate every
significant peak. With both BT and Sonos playing simultaneously, the
correlation should show TWO peaks: one at the BT lag, one at the Sonos lag.
The gap between them is the audible misalignment.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
from scipy import signal as ss


def find_mic_source() -> str | None:
    r = subprocess.run(["pactl", "list", "short", "sources"],
                       capture_output=True, text=True, timeout=4)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and "alsa_input.usb-Jieli" in parts[1]:
            return parts[1]
    return None


def capture_pair(out_dir: Path, dur_sec: float) -> dict | None:
    mic = find_mic_source()
    if not mic:
        print("no Jieli mic found")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    ref_wav = out_dir / f"live_ref_{ts}.wav"
    mic_wav = out_dir / f"live_mic_{ts}.wav"
    common = ["parecord", "--file-format=wav", "--rate=48000",
              "--format=s16le", "--process-time-msec=20"]
    ref_proc = subprocess.Popen(
        common + ["--device=virtual_out.monitor", "--channels=2", str(ref_wav)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    mic_proc = subprocess.Popen(
        common + [f"--device={mic}", "--channels=1", str(mic_wav)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    print(f"capturing for {dur_sec:.1f} s...", flush=True)
    try:
        time.sleep(dur_sec)
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
    return {"ref": ref_wav, "mic": mic_wav}


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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dur", type=float, default=12.0)
    p.add_argument("--keep", action="store_true")
    p.add_argument("--max-lag-ms", type=float, default=6500.0,
                   help="upper end of cross-correlation search window (ms)")
    args = p.parse_args()

    out = capture_pair(Path("/tmp/syncsonic-live"), args.dur)
    if out is None:
        return 2

    ref, ref_sr = load_wav(out["ref"])
    mic, mic_sr = load_wav(out["mic"])
    if ref_sr != mic_sr:
        print(f"sr mismatch ref={ref_sr} mic={mic_sr}")
        return 3
    sr = ref_sr

    print(f"ref dur={len(ref)/sr:.2f}s rms={np.sqrt(np.mean(ref**2)):.4f}")
    print(f"mic dur={len(mic)/sr:.2f}s rms={np.sqrt(np.mean(mic**2)):.4f}")

    # Direct cross-correlation; trim ref/mic to same length
    n = min(len(ref), len(mic))
    ref = ref[:n]
    mic = mic[:n]
    corr = ss.fftconvolve(mic, ref[::-1], mode="full")
    zero_idx = n - 1
    min_lag_ms, max_lag_ms = -100.0, float(args.max_lag_ms)
    min_idx = zero_idx + int(min_lag_ms * sr / 1000.0)
    max_idx = zero_idx + int(max_lag_ms * sr / 1000.0)
    win = corr[min_idx:max_idx + 1]
    norm = np.abs(win)
    print(f"\ncorr window stats: max={norm.max():.4f} mean={norm.mean():.6f}")

    print("\nTop 12 peaks (200 ms guard between picks):")
    guard = int(0.200 * sr)
    work = norm.copy()
    for i in range(12):
        idx = int(np.argmax(work))
        amp = work[idx]
        lag_ms = min_lag_ms + idx * 1000.0 / sr
        ratio = amp / max(norm.mean(), 1e-12)
        print(f"  #{i+1:2d}: lag={lag_ms:7.2f} ms  amp={amp:.4f}  amp/mean={ratio:6.2f}x")
        lo, hi = max(0, idx - guard), min(len(work), idx + guard + 1)
        work[lo:hi] = 0.0

    if not args.keep:
        try:
            out["ref"].unlink()
            out["mic"].unlink()
        except OSError:
            pass
    else:
        print(f"\nkept: {out['ref']}  {out['mic']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
