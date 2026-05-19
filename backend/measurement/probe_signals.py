"""Runtime probe WAV generation for ultrasonic-vs-in-band comparison.

This module is intentionally pure Python + NumPy so slice-0 can generate
repeatable references on a workstation and on the Pi without depending on
the PipeWire runtime. The CLI in ``measurement.runtime_probe_compare`` owns
operator workflow; this file only owns signal construction and WAV I/O.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE_HZ = 48_000
PROBE_DURATION_SEC = 2.65
DEFAULT_ULTRASONIC_HZ = 18_500.0

INBAND_PAUSE_SEC = 0.20
INBAND_CHIRP_START_HZ = 420.0
INBAND_CHIRP_END_HZ = 3_400.0
FADE_SEC = 0.025


def raised_cosine_fade(signal: np.ndarray, sample_rate: int, fade_sec: float = FADE_SEC) -> np.ndarray:
    """Apply symmetric raised-cosine fades to a mono float signal."""
    if signal.ndim != 1:
        raise ValueError("signal must be mono")
    if signal.size == 0:
        return signal.astype(np.float64)
    n_fade = max(1, int(round(fade_sec * sample_rate)))
    if 2 * n_fade >= signal.size:
        n_fade = max(1, signal.size // 4)
    out = signal.astype(np.float64, copy=True)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, math.pi, n_fade, endpoint=False))
    out[:n_fade] *= ramp
    out[-n_fade:] *= ramp[::-1]
    return out


def linear_chirp(
    duration_sec: float,
    sample_rate: int,
    start_hz: float,
    end_hz: float,
) -> np.ndarray:
    """Generate a mono linear-FM sine chirp."""
    n = int(round(duration_sec * sample_rate))
    if n < 16:
        raise ValueError("chirp too short")
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    phase = 2.0 * np.pi * (
        start_hz * t + 0.5 * (end_hz - start_hz) * (t * t) / duration_sec
    )
    return np.sin(phase)


def build_inband_active_probe(
    *,
    sample_rate: int = SAMPLE_RATE_HZ,
    duration_sec: float = PROBE_DURATION_SEC,
    peak: float = 0.40,
) -> np.ndarray:
    """Build the active in-band stimulus: brief pause, chirp, brief pause.

    The operator workflow pauses music before playback, injects this chirp
    into the graph, then resumes music after the capture. The silence is
    part of the reference WAV so hardware captures include clean margins.
    """
    if duration_sec <= 0:
        raise ValueError("duration_sec must be positive")
    if not 0.0 < peak <= 1.0:
        raise ValueError("peak must be in (0, 1]")

    total_n = int(round(duration_sec * sample_rate))
    pause_n = int(round(INBAND_PAUSE_SEC * sample_rate))
    chirp_n = total_n - 2 * pause_n
    if chirp_n < 16:
        raise ValueError("duration too short for in-band chirp plus margins")

    chirp = linear_chirp(
        chirp_n / float(sample_rate),
        sample_rate,
        INBAND_CHIRP_START_HZ,
        INBAND_CHIRP_END_HZ,
    )
    chirp = raised_cosine_fade(chirp, sample_rate)
    max_abs = float(np.max(np.abs(chirp)))
    if max_abs > 0:
        chirp = chirp * (peak / max_abs)

    out = np.zeros(total_n, dtype=np.float64)
    out[pause_n : pause_n + chirp_n] = chirp
    return out


def build_ultrasonic_probe(
    *,
    sample_rate: int = SAMPLE_RATE_HZ,
    duration_sec: float = PROBE_DURATION_SEC,
    frequency_hz: float = DEFAULT_ULTRASONIC_HZ,
    peak: float = 0.95,
) -> np.ndarray:
    """Build a full-amplitude ultrasonic sine probe with soft edges."""
    if duration_sec <= 0:
        raise ValueError("duration_sec must be positive")
    if not 0.0 < frequency_hz < sample_rate / 2.0:
        raise ValueError("frequency_hz must be below Nyquist")
    if not 0.0 < peak <= 1.0:
        raise ValueError("peak must be in (0, 1]")

    n = int(round(duration_sec * sample_rate))
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    sig = np.sin(2.0 * np.pi * frequency_hz * t)
    sig = raised_cosine_fade(sig, sample_rate)
    max_abs = float(np.max(np.abs(sig)))
    if max_abs > 0:
        sig = sig * (peak / max_abs)
    return sig.astype(np.float64)


def write_mono_s16_wav(path: Path, mono: np.ndarray, sample_rate: int = SAMPLE_RATE_HZ) -> None:
    """Write mono float [-1, 1] as a PCM s16 WAV."""
    if mono.ndim != 1:
        raise ValueError("mono WAV writer requires a 1-D signal")
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())


def write_probe_wavs(
    out_dir: Path,
    *,
    ultrasonic_hz: float = DEFAULT_ULTRASONIC_HZ,
    skip_ultrasonic: bool = False,
) -> dict[str, Path]:
    """Generate slice-0 probe WAVs and return their paths by probe name."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    inband = build_inband_active_probe()
    inband_path = out_dir / "syncsonic_inband_active_chirp_48k_mono.wav"
    write_mono_s16_wav(inband_path, inband)
    paths["inband"] = inband_path

    if not skip_ultrasonic:
        ultrasonic = build_ultrasonic_probe(frequency_hz=ultrasonic_hz)
        ultrasonic_path = out_dir / f"syncsonic_ultrasonic_{int(round(ultrasonic_hz))}hz_48k_mono.wav"
        write_mono_s16_wav(ultrasonic_path, ultrasonic)
        paths["ultrasonic"] = ultrasonic_path

    return paths
