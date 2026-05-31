"""Slice 15 unit tests: cross-correlation-on-envelope landmark finder.

All synthetic, no Pi required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from measurement.correlation import build_envelope_template, cross_correlate_envelope, quadratic_peak_interpolation
from measurement.runtime_latency_service import DEFAULT_DURATION_MS, DEFAULT_FREQ_HZ, SAMPLE_RATE, EnvelopeDetector

SR = SAMPLE_RATE
CARRIER = DEFAULT_FREQ_HZ
DURATION_MS = DEFAULT_DURATION_MS


def _synthetic_burst_envelope(offset_samples: float, duration_ms: float = DURATION_MS) -> np.ndarray:
    """Return a demodulated envelope array with a burst near offset_samples."""
    n_burst = int(round(duration_ms * SR / 1000.0))
    n_total = int(round(2.0 * SR))
    raw = np.zeros(n_total, dtype=np.float32)
    offset_int = int(round(offset_samples))
    end = min(n_total, offset_int + n_burst)
    t = np.arange(end - offset_int, dtype=np.float64) / SR
    raw[offset_int:end] = np.sin(2.0 * np.pi * CARRIER * t).astype(np.float32)
    return EnvelopeDetector._demodulated_envelope(raw, 0, CARRIER)


def test_template_synthesis():
    """Template is normalized, bell-shaped, non-trivial."""
    tmpl = build_envelope_template(DURATION_MS, CARRIER, SR)
    assert len(tmpl) > 0
    assert abs(float(np.max(tmpl)) - 1.0) < 0.01, "template not normalized to 1"
    peak_pos = int(np.argmax(tmpl))
    center = len(tmpl) / 2.0
    assert abs(peak_pos - center) < 0.15 * len(tmpl), f"template peak {peak_pos} not near center {center:.0f}"
    left_energy = float(np.sum(tmpl[: len(tmpl) // 2] ** 2))
    right_energy = float(np.sum(tmpl[len(tmpl) // 2 :] ** 2))
    asymmetry = abs(left_energy - right_energy) / max(left_energy, right_energy)
    assert asymmetry < 0.15, f"template too asymmetric: {asymmetry:.3f}"


def test_xcorr_perfect_integer_burst():
    """Clean synthetic burst -> xcorr peak within 0.5 samples of ground truth."""
    ground_truth = 500
    env = _synthetic_burst_envelope(ground_truth)
    tmpl = build_envelope_template(DURATION_MS, CARRIER, SR)
    xcorr, _ = cross_correlate_envelope(env, tmpl)
    zero_lag = len(tmpl) - 1
    peak_idx = int(np.argmax(np.abs(xcorr)))
    frac = quadratic_peak_interpolation(xcorr, peak_idx)
    recovered = frac - zero_lag
    assert abs(recovered - ground_truth) < 0.5, (
        f"peak recovery error {abs(recovered - ground_truth):.3f} samples exceeds 0.5"
    )


def test_xcorr_subsample_recovery():
    """Known fractional-sample offset recovered within 0.5 samples via quadratic interp."""
    ground_truth_frac = 1000.3
    env = _synthetic_burst_envelope(ground_truth_frac)
    tmpl = build_envelope_template(DURATION_MS, CARRIER, SR)
    xcorr, _ = cross_correlate_envelope(env, tmpl)
    zero_lag = len(tmpl) - 1
    peak_idx = int(np.argmax(np.abs(xcorr)))
    frac_peak = quadratic_peak_interpolation(xcorr, peak_idx)
    recovered = frac_peak - zero_lag
    assert abs(recovered - ground_truth_frac) < 0.5, (
        f"sub-sample recovery error {abs(recovered - ground_truth_frac):.4f} exceeds 0.5 samples"
    )


def test_xcorr_attenuated_noisy_burst():
    """Attenuated+noisy burst simulating BT codec path: position within 2 samples."""
    rng = np.random.default_rng(seed=15_01)
    ground_truth = 700
    env = _synthetic_burst_envelope(ground_truth)
    env_att = env * 0.1
    burst_rms = float(np.sqrt(np.mean(env_att ** 2)))
    noise_rms = burst_rms / (10 ** (15.0 / 20.0))
    env_noisy = env_att + rng.standard_normal(len(env_att)) * noise_rms
    tmpl = build_envelope_template(DURATION_MS, CARRIER, SR)
    xcorr, _ = cross_correlate_envelope(env_noisy, tmpl)
    zero_lag = len(tmpl) - 1
    peak_idx = int(np.argmax(np.abs(xcorr)))
    recovered = quadratic_peak_interpolation(xcorr, peak_idx) - zero_lag
    assert abs(recovered - ground_truth) < 2.0, (
        f"noisy recovery error {abs(recovered - ground_truth):.2f} samples exceeds 2"
    )
    peak_val = float(np.abs(xcorr[peak_idx]))
    half_val = peak_val * 0.5
    lw, rw = peak_idx, peak_idx
    while lw > 0 and abs(xcorr[lw]) >= half_val:
        lw -= 1
    while rw < len(xcorr) - 1 and abs(xcorr[rw]) >= half_val:
        rw += 1
    fwhm_samples = max(1, rw - lw)
    precision_us = float(fwhm_samples) * 1e6 / SR
    assert precision_us < 5000.0, f"precision_us={precision_us:.0f} unreasonably large"


def test_xcorr_three_burst_pattern():
    """Three-burst pattern: scan returns 3 candidates within 1 sample each."""
    ground_truths_ms = [200, 500, 800]
    ground_truths_samples = [int(round(t * SR / 1000.0)) for t in ground_truths_ms]
    n_total = int(1.1 * SR)
    raw = np.zeros(n_total, dtype=np.float32)
    n_burst = int(round(DURATION_MS * SR / 1000.0))
    for gt in ground_truths_samples:
        end = min(n_total, gt + n_burst)
        t = np.arange(end - gt, dtype=np.float64) / SR
        raw[gt:end] += np.sin(2.0 * np.pi * CARRIER * t).astype(np.float32)
    candidates, _ = EnvelopeDetector._demodulated_envelope_scan(
        raw, 0, carrier_hz=CARRIER, min_snr_db=6.0
    )
    assert len(candidates) == 3, f"expected 3 candidates, got {len(candidates)}"
    for i, (cand, gt) in enumerate(zip(candidates, ground_truths_samples)):
        err = abs(cand["sample_index"] - gt)
        assert err <= 1, f"burst {i}: error {err} samples > 1, gt={gt}, got={cand['sample_index']}"
        assert "precision_us" in cand, f"burst {i} missing precision_us"
        assert cand["precision_us"] < 500.0, f"burst {i} precision_us={cand['precision_us']:.1f}us > 500"


def test_measurement_precision_us_in_analyze_pattern():
    """Full analyze_pattern_in_samples returns measurement_precision_us in selected dict."""
    burst_gap_samples = int(round(300 * SR / 1000.0))
    gt0 = int(round(200 * SR / 1000.0))
    gt1 = gt0 + burst_gap_samples
    n_total = int(round(1.0 * SR))
    raw = np.zeros(n_total, dtype=np.float32)
    n_burst = int(round(DURATION_MS * SR / 1000.0))
    for gt in (gt0, gt1):
        end = min(n_total, gt + n_burst)
        t = np.arange(end - gt, dtype=np.float64) / SR
        raw[gt:end] += np.sin(2.0 * np.pi * CARRIER * t).astype(np.float32)
    emit_frame_indices = [gt0, gt1]
    analysis = EnvelopeDetector.analyze_pattern_in_samples(
        raw,
        0.0,
        0,
        -90.0,
        emit_frame_indices,
        tolerance_ms=50.0,
        min_snr_db=6.0,
        landmark="envelope",
        carrier_hz=CARRIER,
    )
    assert "selected" in analysis, f"no selection: {analysis.get('reject_reason')}"
    selected = analysis["selected"]
    assert "measurement_precision_us" in selected, "measurement_precision_us missing from selected"
    prec = selected["measurement_precision_us"]
    assert isinstance(prec, float) and prec > 0, f"precision_us={prec!r} not positive float"
    assert prec < 2000.0, f"precision_us={prec:.1f}us unreasonably large for clean synthetic input"
