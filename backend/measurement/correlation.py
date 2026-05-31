"""Shared correlation helpers for SyncSonic measurement paths."""

from __future__ import annotations

import numpy as np


def _fft_correlate_full(envelope: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Return scipy.signal.correlate(..., mode="full") compatible output."""
    n = len(envelope)
    m = len(template)
    if n == 0 or m == 0:
        return np.zeros(max(0, n + m - 1), dtype=np.float64)
    out_len = n + m - 1
    fft_len = 1 << (out_len - 1).bit_length()
    spectrum_a = np.fft.fft(envelope, fft_len)
    spectrum_b = np.fft.fft(template[::-1], fft_len)
    return np.fft.ifft(spectrum_a * spectrum_b).real[:out_len]


def _moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or len(values) < window:
        return values
    kernel = np.ones(window, dtype=np.float64) / window
    return np.convolve(values, kernel, mode="same")


def build_envelope_template(
    burst_duration_ms: float,
    carrier_hz: float,
    sample_rate: int,
    mix_low_pass_ms: float = 1.5,
    smooth_ms: float = 2.0,
) -> np.ndarray:
    """Synthesize a demodulated reference burst envelope.

    The template is a rectangular ultrasonic pulse passed through the same
    IQ demodulator, low-pass, and smooth stages as
    ``EnvelopeDetector._demodulated_envelope()``. The 10 ms margins keep the
    convolution edge response out of the usable template; the returned array
    is trimmed to the burst support, float64, and peak-normalized to 1.0.
    """
    n_burst = int(round(burst_duration_ms * sample_rate / 1000.0))
    n_total = n_burst + 2 * int(round(10 * sample_rate / 1000.0))
    offset = int(round(10 * sample_rate / 1000.0))
    raw = np.zeros(n_total, dtype=np.float32)
    t = np.arange(n_burst, dtype=np.float64) / sample_rate
    raw[offset : offset + n_burst] = np.sin(2.0 * np.pi * carrier_hz * t).astype(np.float32)

    phase = -2.0 * np.pi * carrier_hz * np.arange(n_total, dtype=np.float64) / sample_rate
    mixed = raw.astype(np.float64) * np.exp(1j * phase)
    mix_window = max(1, int(round(mix_low_pass_ms * sample_rate / 1000.0)))
    mixed = np.convolve(mixed, np.ones(mix_window, dtype=np.float64) / mix_window, mode="same")
    envelope = 2.0 * np.abs(mixed)
    smooth_window = max(1, int(round(smooth_ms * sample_rate / 1000.0)))
    if smooth_window > 1:
        envelope = np.convolve(envelope, np.ones(smooth_window, dtype=np.float64) / smooth_window, mode="same")
    envelope = envelope[offset : offset + n_burst]
    peak = float(np.max(envelope))
    if peak > 0:
        envelope = envelope / peak
    return envelope.astype(np.float64)


def quadratic_peak_interpolation(corr: np.ndarray, peak_idx: int) -> float:
    """Return a sub-sample peak position using a 3-point parabolic fit."""
    if peak_idx <= 0 or peak_idx >= len(corr) - 1:
        return float(peak_idx)
    alpha = float(corr[peak_idx - 1])
    beta = float(corr[peak_idx])
    gamma = float(corr[peak_idx + 1])
    denom = alpha - 2.0 * beta + gamma
    if abs(denom) < 1e-15:
        return float(peak_idx)
    offset = 0.5 * (alpha - gamma) / denom
    offset = max(-0.5, min(0.5, offset))
    return float(peak_idx) + offset


def cross_correlate_envelope(
    envelope: np.ndarray,
    template: np.ndarray,
) -> tuple[np.ndarray, float]:
    """FFT cross-correlate an envelope against a template.

    Returns ``(corr_array, peak_corr_normalized)`` where ``corr_array[k]``
    is the similarity of ``envelope`` shifted by ``k`` samples relative to
    ``template``.
    """
    envelope64 = envelope.astype(np.float64)
    template64 = template.astype(np.float64)
    # Correlate on envelope shape changes rather than DC burst energy. A raw
    # rectangular-envelope match has a very broad triangular lobe; the gradient
    # keeps the same synthetic-template contract while making the arrival edge
    # the timing landmark.
    gradient_smooth = max(1, min(48, len(template64) // 100))
    corr_envelope = np.gradient(_moving_average(envelope64, gradient_smooth))
    corr_template = np.gradient(_moving_average(template64, gradient_smooth))
    try:
        from scipy.signal import correlate

        full = correlate(corr_envelope, corr_template, mode="full", method="fft")
    except ImportError:  # pragma: no cover - Pi/runtime env declares scipy; local fallback keeps tests runnable.
        full = _fft_correlate_full(corr_envelope, corr_template)
    if len(full) > 1:
        full = np.concatenate([full[1:], np.zeros(1, dtype=np.float64)])
    template_energy = float(np.sum(corr_template ** 2))
    peak_norm = float(np.max(np.abs(full))) / max(template_energy, 1e-12)
    return full, peak_norm
