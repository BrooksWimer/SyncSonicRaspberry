"""Per-speaker EQ measurement and inverse-profile generation.

The runtime path is intentionally simple: play a logarithmic sine sweep
through one target sink, mute the other sinks for the measurement window,
capture the USB mic with ``parecord`` at 48 kHz mono, estimate the
speaker response, smooth it at roughly 1/6 octave, and write
``backend/eq_profiles/<mac>.json`` for ``pw_eq_filter``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

SAMPLE_RATE_HZ = 48_000
MIN_EQ_HZ = 60.0
MAX_EQ_HZ = 16_000.0
MAX_CUT_DB = -12.0
MAX_BOOST_DB = 6.0
DEFAULT_SWEEP_SEC = 6.0
DEFAULT_AMPLITUDE = 0.35
PROFILE_DIR = Path(__file__).resolve().parents[1] / "eq_profiles"
BASELINE_DIR = Path(__file__).resolve().parents[1] / "eq_measurements"


def _ensure_syncsonic_audio_env() -> None:
    runtime_dir = os.environ.get("RUNTIME_DIRECTORY", "/run/syncsonic")
    os.environ["XDG_RUNTIME_DIR"] = runtime_dir
    os.environ["PULSE_SERVER"] = "unix:/run/syncsonic/pulse/native"
    os.environ["PULSE_SYSTEM_BUS"] = "1"
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"


@dataclass(frozen=True)
class EqBand:
    freq_hz: float
    gain_db: float
    q: float
    b0: float
    b1: float
    b2: float
    a1: float
    a2: float


def sanitize_mac(mac: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in mac.upper())


def build_log_sweep(
    *,
    sample_rate: int = SAMPLE_RATE_HZ,
    duration_sec: float = DEFAULT_SWEEP_SEC,
    start_hz: float = MIN_EQ_HZ,
    stop_hz: float = MAX_EQ_HZ,
    amplitude: float = DEFAULT_AMPLITUDE,
) -> np.ndarray:
    n = int(round(sample_rate * duration_sec))
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    ratio = stop_hz / start_hz
    phase = 2.0 * np.pi * start_hz * duration_sec / math.log(ratio) * (
        np.power(ratio, t / duration_sec) - 1.0
    )
    sweep = np.sin(phase) * float(amplitude)
    fade = max(1, int(0.02 * sample_rate))
    window = np.ones(n, dtype=np.float64)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, fade))
    window[:fade] = ramp
    window[-fade:] = ramp[::-1]
    return sweep * window


def deconvolve_response(
    reference: np.ndarray,
    captured: np.ndarray,
    *,
    sample_rate: int = SAMPLE_RATE_HZ,
    n_fft: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    ref = np.asarray(reference, dtype=np.float64)
    cap = np.asarray(captured, dtype=np.float64)
    n = int(n_fft or 1 << int(math.ceil(math.log2(max(ref.size, cap.size)))))
    ref_f = np.fft.rfft(ref, n=n)
    cap_f = np.fft.rfft(cap, n=n)
    eps = 1.0e-10 * max(1.0, float(np.max(np.abs(ref_f))))
    h = cap_f * np.conj(ref_f) / (np.abs(ref_f) ** 2 + eps)
    freqs = np.fft.rfftfreq(n, d=1.0 / float(sample_rate))
    mag_db = 20.0 * np.log10(np.maximum(np.abs(h), 1.0e-12))
    return freqs, mag_db


def welch_transfer_response(
    reference: np.ndarray,
    captured: np.ndarray,
    *,
    sample_rate: int = SAMPLE_RATE_HZ,
    nperseg: int = 4096,
) -> tuple[np.ndarray, np.ndarray]:
    try:
        from scipy import signal as ss

        freqs, pxy = ss.csd(captured, reference, fs=sample_rate, nperseg=nperseg)
        _, pxx = ss.welch(reference, fs=sample_rate, nperseg=nperseg)
        h = pxy / np.maximum(pxx, 1.0e-18)
        return freqs, 20.0 * np.log10(np.maximum(np.abs(h), 1.0e-12))
    except Exception:
        return deconvolve_response(reference, captured, sample_rate=sample_rate)


def smooth_fractional_octave(
    freqs: np.ndarray,
    mag_db: np.ndarray,
    *,
    fraction: float = 6.0,
    min_hz: float = MIN_EQ_HZ,
    max_hz: float = MAX_EQ_HZ,
    points: int = 96,
) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.asarray(freqs, dtype=np.float64)
    mag_db = np.asarray(mag_db, dtype=np.float64)
    centers = np.geomspace(min_hz, max_hz, points)
    smoothed = np.zeros_like(centers)
    width = 2.0 ** (1.0 / (2.0 * fraction))
    for i, center in enumerate(centers):
        lo = center / width
        hi = center * width
        mask = (freqs >= lo) & (freqs <= hi)
        if not np.any(mask):
            smoothed[i] = float(np.interp(center, freqs, mag_db))
        else:
            smoothed[i] = float(np.mean(mag_db[mask]))
    return centers, smoothed


def derive_inverse_curve(
    freqs: np.ndarray,
    response_db: np.ndarray,
    *,
    max_cut_db: float = MAX_CUT_DB,
    max_boost_db: float = MAX_BOOST_DB,
) -> np.ndarray:
    response_db = np.asarray(response_db, dtype=np.float64)
    neutral = response_db - float(np.median(response_db))
    inverse = -neutral
    return np.clip(inverse, max_cut_db, max_boost_db)


def rbj_peaking_coefficients(
    freq_hz: float,
    gain_db: float,
    q: float,
    *,
    sample_rate: int = SAMPLE_RATE_HZ,
) -> dict[str, float]:
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * freq_hz / float(sample_rate)
    alpha = math.sin(w0) / (2.0 * q)
    cos_w0 = math.cos(w0)
    b0 = 1.0 + alpha * a
    b1 = -2.0 * cos_w0
    b2 = 1.0 - alpha * a
    a0 = 1.0 + alpha / a
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha / a
    return {
        "b0": b0 / a0,
        "b1": b1 / a0,
        "b2": b2 / a0,
        "a1": a1 / a0,
        "a2": a2 / a0,
    }


def build_eq_bands(
    freqs: np.ndarray,
    inverse_db: np.ndarray,
    *,
    min_abs_gain_db: float = 0.75,
    q: float = 4.318,
) -> list[EqBand]:
    bands: list[EqBand] = []
    for freq, gain in zip(freqs, inverse_db):
        if abs(float(gain)) < min_abs_gain_db:
            continue
        coeff = rbj_peaking_coefficients(float(freq), float(gain), q)
        bands.append(EqBand(float(freq), float(gain), float(q), **coeff))
    return bands


def write_profile(mac: str, bands: Sequence[EqBand], *, out_dir: Path = PROFILE_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sanitize_mac(mac)}.json"
    payload = {
        "schema": "syncsonic.eq_profile.v1",
        "mac": mac.upper(),
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "bands": [
            {
                "enabled": True,
                "type": "peaking",
                "freq_hz": band.freq_hz,
                "gain_db": band.gain_db,
                "q": band.q,
                "b0": band.b0,
                "b1": band.b1,
                "b2": band.b2,
                "a1": band.a1,
                "a2": band.a2,
            }
            for band in bands
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_baseline(
    mac: str,
    sink_name: str,
    *,
    centers: np.ndarray,
    response_db: np.ndarray,
    inverse_db: np.ndarray,
    sweep_sec: float,
    mic_source: str | None,
    out_dir: Path = BASELINE_DIR,
    measurement_mode: str = "per_speaker_isolated",
    filename_stem: str | None = None,
) -> Path:
    """Persist smoothed per-speaker response for later system-wide inference."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = filename_stem or sanitize_mac(mac)
    path = out_dir / f"{stem}.baseline.json"
    payload = {
        "schema": "syncsonic.eq_baseline.v1",
        "mac": mac.upper(),
        "sink": sink_name,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "sweep_sec": sweep_sec,
        "mic_source": mic_source,
        "measurement_mode": measurement_mode,
        "freq_hz": [float(x) for x in centers],
        "response_db": [float(x) for x in response_db],
        "inverse_db": [float(x) for x in inverse_db],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        sample_rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit PCM WAV, got sample width {width}")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate


def _write_wav_mono(path: Path, samples: np.ndarray, sample_rate: int = SAMPLE_RATE_HZ) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    raw = (pcm * 32767.0).astype(np.int16).tobytes()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(raw)


def _list_sinks() -> list[str]:
    result = subprocess.run(["pactl", "list", "sinks", "short"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "pactl list sinks failed").strip())
    sinks: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            sinks.append(parts[1])
    return sinks


def _set_sink_mute(sink: str, muted: bool) -> None:
    subprocess.run(["pactl", "set-sink-mute", sink, "1" if muted else "0"], check=True)


def _get_sink_mute(sink: str) -> bool:
    result = subprocess.run(["pactl", "get-sink-mute", sink], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or f"pactl get-sink-mute failed for {sink}").strip())
    return "yes" in result.stdout.lower()


def _play_wav(path: Path, sink: str) -> None:
    subprocess.run(["paplay", "--device", sink, str(path)], check=True)


@dataclass(frozen=True)
class MeasurementResult:
    profile_path: Path
    baseline_path: Path
    band_count: int


def measure_and_write_profile(
    mac: str,
    sink_name: str,
    *,
    mic_source: str | None = None,
    sweep_sec: float = DEFAULT_SWEEP_SEC,
    out_dir: Path = PROFILE_DIR,
    baseline_dir: Path = BASELINE_DIR,
    measurement_mode: str = "per_speaker_isolated",
    mute_other_sinks: bool = True,
    baseline_stem: str | None = None,
    save_profile: bool = True,
) -> MeasurementResult:
    sweep = build_log_sweep(duration_sec=sweep_sec)
    sinks = _list_sinks()
    restore: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="syncsonic-eq-") as td:
        tmp = Path(td)
        sweep_path = tmp / "sweep.wav"
        capture_path = tmp / "capture.wav"
        _write_wav_mono(sweep_path, sweep)

        for sink in sinks:
            if mute_other_sinks and sink != sink_name:
                restore[sink] = _get_sink_mute(sink)
                _set_sink_mute(sink, True)
        try:
            cap_proc = subprocess.Popen([
                "parecord",
                "--file-format=wav",
                "--rate=48000",
                "--channels=1",
                *(["--device", mic_source] if mic_source else []),
                str(capture_path),
            ])
            try:
                _play_wav(sweep_path, sink_name)
            finally:
                try:
                    cap_proc.send_signal(2)
                    cap_proc.wait(timeout=2.0)
                except Exception:
                    cap_proc.kill()
        finally:
            for sink, was_muted in restore.items():
                _set_sink_mute(sink, was_muted)

        captured, sample_rate = _read_wav_mono(capture_path)
    if sample_rate != SAMPLE_RATE_HZ:
        raise ValueError(f"expected {SAMPLE_RATE_HZ} Hz capture, got {sample_rate}")

    freqs, response = deconvolve_response(sweep, captured, sample_rate=SAMPLE_RATE_HZ)
    if not np.all(np.isfinite(response)):
        freqs, response = welch_transfer_response(sweep, captured, sample_rate=SAMPLE_RATE_HZ)
    centers, smooth_db = smooth_fractional_octave(freqs, response)
    inverse_db = derive_inverse_curve(centers, smooth_db)
    bands = build_eq_bands(centers, inverse_db)
    profile_path = out_dir / f"{sanitize_mac(mac)}.json"
    if save_profile:
        profile_path = write_profile(mac, bands, out_dir=out_dir)
    baseline_path = write_baseline(
        mac,
        sink_name,
        centers=centers,
        response_db=smooth_db,
        inverse_db=inverse_db,
        sweep_sec=sweep_sec,
        mic_source=mic_source,
        out_dir=baseline_dir,
        measurement_mode=measurement_mode,
        filename_stem=baseline_stem,
    )
    return MeasurementResult(profile_path, baseline_path, len(bands))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mac", default=None)
    parser.add_argument("--sink", default=None, help="PipeWire/Pulse sink for the target speaker")
    parser.add_argument("--mic-source", default=None)
    parser.add_argument("--sweep-sec", type=float, default=DEFAULT_SWEEP_SEC)
    parser.add_argument("--out-dir", type=Path, default=PROFILE_DIR)
    parser.add_argument("--baseline-dir", type=Path, default=BASELINE_DIR)
    parser.add_argument(
        "--combined",
        action="store_true",
        help="Play through virtual_out with all speakers unmuted (system validation pass)",
    )
    parser.add_argument(
        "--combined-label",
        default="combined.after_eq",
        help="Baseline filename stem when --combined is set",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    _ensure_syncsonic_audio_env()
    if args.combined:
        result = measure_and_write_profile(
            "COMBINED",
            "virtual_out",
            mic_source=args.mic_source,
            sweep_sec=args.sweep_sec,
            out_dir=args.out_dir,
            baseline_dir=args.baseline_dir,
            measurement_mode="combined_all_speakers",
            mute_other_sinks=False,
            baseline_stem=args.combined_label,
            save_profile=False,
        )
        payload = json.loads(result.baseline_path.read_text(encoding="utf-8"))
        span = max(payload["response_db"]) - min(payload["response_db"])
        print(result.baseline_path)
        print(f"response_span_db={span:.1f}")
        return 0

    if not args.mac or not args.sink:
        parser.error("--mac and --sink are required unless --combined is set")

    result = measure_and_write_profile(
        args.mac,
        args.sink,
        mic_source=args.mic_source,
        sweep_sec=args.sweep_sec,
        out_dir=args.out_dir,
        baseline_dir=args.baseline_dir,
    )
    print(result.profile_path)
    print(result.baseline_path)
    print(f"bands={result.band_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
