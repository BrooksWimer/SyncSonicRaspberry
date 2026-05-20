from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from measurement.probe_signals import (  # noqa: E402
    DEFAULT_ULTRASONIC_HZ,
    PROBE_DURATION_SEC,
    SAMPLE_RATE_HZ,
    build_ultrasonic_probe,
    write_probe_wavs,
)
from measurement.runtime_probe_compare import hf_power_db  # noqa: E402


def _read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        sr = wav.getframerate()
        assert wav.getnchannels() == 1
        raw = wav.readframes(wav.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0, sr


def test_inband_wav_generation_produces_48k_mono_roughly_2_65s(tmp_path: Path) -> None:
    paths = write_probe_wavs(tmp_path, skip_ultrasonic=True)

    with wave.open(str(paths["inband"]), "rb") as wav:
        assert wav.getframerate() == SAMPLE_RATE_HZ
        assert wav.getnchannels() == 1
        duration = wav.getnframes() / float(wav.getframerate())

    assert abs(duration - PROBE_DURATION_SEC) < 0.005


def test_ultrasonic_wav_fft_peak_is_within_500_hz_of_18_5khz(tmp_path: Path) -> None:
    paths = write_probe_wavs(tmp_path, ultrasonic_hz=DEFAULT_ULTRASONIC_HZ)
    samples, sr = _read_wav_mono(paths["ultrasonic"])

    spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / float(sr))
    peak_hz = float(freqs[int(np.argmax(spectrum))])

    assert abs(peak_hz - DEFAULT_ULTRASONIC_HZ) <= 500.0


def test_hf_power_db_reports_near_zero_dbfs_for_pure_18_5khz_tone() -> None:
    tone = build_ultrasonic_probe(
        sample_rate=SAMPLE_RATE_HZ,
        duration_sec=1.0,
        frequency_hz=DEFAULT_ULTRASONIC_HZ,
        peak=1.0,
    )

    assert abs(hf_power_db(tone, SAMPLE_RATE_HZ, center_hz=DEFAULT_ULTRASONIC_HZ)) < 0.5
