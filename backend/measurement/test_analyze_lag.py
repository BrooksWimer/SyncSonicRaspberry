"""Synthetic ground-truth tests for the Slice 4.1 lag analyzer.

Runs without a Pi. Each test constructs a synthetic reference signal,
delays it by a known integer-sample amount (and optionally adds noise
or attenuation to simulate the acoustic path), then asserts the
analyzer recovers the exact delay within +/- 1 sample. If any test
fails, the analyzer is unfit for purpose and Slice 4.2 (the
calibration loop) must not be deployed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# Allow running this file directly: ``python -m measurement.test_analyze_lag``
# or even ``python backend/measurement/test_analyze_lag.py`` from any cwd.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from measurement.analyze_lag import estimate_lag_samples  # noqa: E402

SR = 48000


def _pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """Approximate pink noise via 1/f-shaped white noise. Deterministic
    given a seeded ``rng``. Only used to feed the analyzer realistic
    (broadband, music-like) signals; the exact spectrum doesn't matter
    for correctness."""
    white = rng.standard_normal(n)
    f = np.fft.rfft(white)
    freqs = np.arange(1, len(f) + 1, dtype=np.float64)
    f /= np.sqrt(freqs)
    return np.fft.irfft(f, n=n)


def _delay_signal(signal: np.ndarray, lag_samples: int, attenuation_db: float = 0.0) -> np.ndarray:
    """Return a copy of ``signal`` delayed by ``lag_samples`` and
    optionally attenuated. Same length as the input; head is zero-
    padded, tail is truncated. Models a "speaker emits the reference
    audio after lag samples, attenuated by N dB on the way to the mic."""
    out = np.zeros_like(signal, dtype=np.float64)
    if lag_samples >= 0:
        if lag_samples < len(signal):
            out[lag_samples:] = signal[: len(signal) - lag_samples]
    else:
        # Negative lag = signal arrives EARLY. Should not be physical,
        # but the analyzer should still recover it.
        k = -lag_samples
        if k < len(signal):
            out[: len(signal) - k] = signal[k:]
    gain = 10.0 ** (attenuation_db / 20.0)
    return out * gain


def _add_noise(signal: np.ndarray, snr_db: float, rng: np.random.Generator) -> np.ndarray:
    """Add white noise at the requested SNR. SNR is relative to the
    signal RMS; matches how a mic with constant noise floor sees a
    speaker output of varying loudness."""
    sig_rms = float(np.sqrt(np.mean(signal * signal)))
    if sig_rms == 0:
        return signal + rng.standard_normal(len(signal))
    noise_rms = sig_rms / (10.0 ** (snr_db / 20.0))
    return signal + noise_rms * rng.standard_normal(len(signal))


def _check(test_name: str, expected_lag: int, est, tol_samples: int = 1) -> None:
    delta = est.lag_samples - expected_lag
    print(f"{test_name}:")
    print(f"  expected lag: {expected_lag} samples ({expected_lag*1000.0/SR:+.2f} ms)")
    print(f"  got lag:      {est.lag_samples} samples ({est.lag_ms:+.2f} ms)")
    print(f"  delta:        {delta} samples")
    print(f"  peak r:       {est.peak_correlation:+.4f}")
    print(f"  conf 1/2:     {est.confidence_primary:.2f}x / {est.confidence_secondary:.2f}x")
    if abs(delta) > tol_samples:
        raise AssertionError(
            f"{test_name}: lag delta {delta} exceeds tolerance +/-{tol_samples}"
        )
    print(f"  PASS\n")


def test_perfect_delay():
    """Identical reference and captured, captured delayed by exactly 1000 samples
    (~20.83 ms at 48 kHz). No noise, no attenuation. The analyzer should
    return exactly 1000."""
    rng = np.random.default_rng(seed=42)
    ref = _pink_noise(5 * SR, rng)
    cap = _delay_signal(ref, lag_samples=1000)
    est = estimate_lag_samples(ref, cap, sample_rate=SR)
    _check("test_perfect_delay (1000 samples, no noise)", 1000, est, tol_samples=0)


def test_attenuated_delay():
    """Realistic speaker-to-listener path: -30 dB attenuation, otherwise
    clean. Should still recover exact lag because there's no noise."""
    rng = np.random.default_rng(seed=43)
    ref = _pink_noise(5 * SR, rng)
    cap = _delay_signal(ref, lag_samples=4800, attenuation_db=-30.0)
    est = estimate_lag_samples(ref, cap, sample_rate=SR)
    _check("test_attenuated_delay (4800 samples = 100 ms, -30 dB)", 4800, est, tol_samples=0)


def test_noisy_attenuated_delay():
    """Realistic mic capture: -30 dB attenuation + +20 dB SNR ambient noise.
    Should still recover lag within +/- 1 sample and report high confidence."""
    rng = np.random.default_rng(seed=44)
    ref = _pink_noise(5 * SR, rng)
    delayed = _delay_signal(ref, lag_samples=2400, attenuation_db=-30.0)
    cap = _add_noise(delayed, snr_db=20.0, rng=rng)
    est = estimate_lag_samples(ref, cap, sample_rate=SR)
    _check("test_noisy_attenuated_delay (2400 samples = 50 ms, -30 dB, 20 dB SNR)",
           2400, est, tol_samples=1)
    assert est.confidence_secondary > 2.0, (
        f"confidence secondary {est.confidence_secondary} below 2.0x; "
        "would be ambiguous in production"
    )


def test_low_snr_should_still_work():
    """Hard case: -40 dB attenuation + 5 dB SNR. The peak should still
    win but confidence will be lower. We allow looser tolerance here.
    Failure indicates the analyzer can't survive realistic mic
    conditions and we need a different approach (e.g. a known
    reference signal rather than music)."""
    rng = np.random.default_rng(seed=45)
    ref = _pink_noise(8 * SR, rng)  # longer for better statistics
    delayed = _delay_signal(ref, lag_samples=1500, attenuation_db=-40.0)
    cap = _add_noise(delayed, snr_db=5.0, rng=rng)
    est = estimate_lag_samples(ref, cap, sample_rate=SR)
    _check("test_low_snr (1500 samples = 31 ms, -40 dB, 5 dB SNR, 8 sec window)",
           1500, est, tol_samples=2)


def test_zero_delay():
    """Reference == captured. Should return lag=0, near-perfect Pearson r."""
    rng = np.random.default_rng(seed=46)
    ref = _pink_noise(3 * SR, rng)
    est = estimate_lag_samples(ref, ref, sample_rate=SR)
    _check("test_zero_delay (identical signals)", 0, est, tol_samples=0)
    assert est.peak_correlation > 0.99, (
        f"peak correlation {est.peak_correlation} should be ~1 for identical signals"
    )


def test_negative_delay():
    """Captured leading reference (would be unphysical for a real mic
    capture but tests the search-window math). Reference is a copy of
    captured delayed by 200 samples, so captured itself is reference
    with -200 lag."""
    rng = np.random.default_rng(seed=47)
    ref = _pink_noise(3 * SR, rng)
    cap = _delay_signal(ref, lag_samples=-200)
    est = estimate_lag_samples(ref, cap, sample_rate=SR)
    _check("test_negative_delay (-200 samples)", -200, est, tol_samples=0)


def test_overlap_window_with_silence():
    """First 2 seconds of captured are silence, then the delayed
    reference. Models 'mic was rolling before the speaker started'.
    Analyzer should still find the right lag from the overlapping part."""
    rng = np.random.default_rng(seed=48)
    ref = _pink_noise(5 * SR, rng)
    delayed = _delay_signal(ref, lag_samples=600, attenuation_db=-25.0)
    cap = np.concatenate([np.zeros(2 * SR), delayed])
    # cap is now reference shifted by (2*SR + 600) samples
    expected_lag = 2 * SR + 600
    est = estimate_lag_samples(
        ref, cap, sample_rate=SR,
        max_lag_ms=2500.0,  # extend window above default to cover 2 sec offset
    )
    _check("test_overlap_with_2s_leading_silence",
           expected_lag, est, tol_samples=0)


if __name__ == "__main__":
    print(f"=== Slice 4.1 analyzer synthetic tests (SR={SR} Hz) ===\n")
    tests = [
        test_perfect_delay,
        test_attenuated_delay,
        test_noisy_attenuated_delay,
        test_low_snr_should_still_work,
        test_zero_delay,
        test_negative_delay,
        test_overlap_window_with_silence,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            print(f"  FAIL: {exc}\n")
            failures += 1
    if failures:
        print(f"=== {failures}/{len(tests)} tests FAILED ===")
        sys.exit(1)
    print(f"=== ALL {len(tests)} tests passed ===")
