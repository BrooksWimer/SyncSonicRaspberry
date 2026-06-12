from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from measurement.eq_measurement import (  # noqa: E402
    SAMPLE_RATE_HZ,
    deconvolve_response,
    derive_inverse_curve,
    smooth_fractional_octave,
)


def _tone(freq_hz: float, duration_sec: float = 2.0) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE_HZ * duration_sec), dtype=np.float64) / SAMPLE_RATE_HZ
    return np.sin(2.0 * np.pi * freq_hz * t)


def test_deconvolution_recovers_known_gain_at_tone_frequency() -> None:
    ref = _tone(1000.0)
    gain = 10.0 ** (-6.0 / 20.0)
    captured = ref * gain

    freqs, mag_db = deconvolve_response(ref, captured)
    idx = int(np.argmin(np.abs(freqs - 1000.0)))

    assert abs(float(mag_db[idx]) - (-6.0)) < 0.2


def test_one_sixth_octave_smoothing_reduces_narrow_spike() -> None:
    freqs = np.linspace(20.0, 20_000.0, 20_000)
    response = np.zeros_like(freqs)
    response[np.argmin(np.abs(freqs - 1000.0))] = 18.0

    centers, smooth = smooth_fractional_octave(freqs, response, points=80)
    near = int(np.argmin(np.abs(centers - 1000.0)))

    assert smooth[near] < 2.0
    assert abs(float(np.median(smooth))) < 0.05


def test_inverse_curve_is_median_referenced_and_bounded() -> None:
    response = np.array([-30.0, -9.0, 0.0, 9.0, 30.0])
    inverse = derive_inverse_curve(np.arange(response.size), response)

    assert np.max(inverse) <= 6.0
    assert np.min(inverse) >= -12.0
    assert inverse[2] == 0.0
    assert inverse[0] == 6.0
    assert inverse[-1] == -12.0
