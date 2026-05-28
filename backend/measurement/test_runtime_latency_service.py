from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

sys.modules.setdefault("dbus", types.SimpleNamespace(SystemBus=lambda: None))

from measurement.runtime_latency_service import (  # noqa: E402
    EnvelopeDetector,
    RingBuffer,
    SAMPLE_RATE,
)


def _tone(start: int, duration: int, total: int, freq_hz: float = 18_500.0) -> np.ndarray:
    samples = np.zeros(total, dtype=np.float32)
    t = np.arange(duration, dtype=np.float64) / SAMPLE_RATE
    samples[start : start + duration] = (0.8 * np.sin(2.0 * np.pi * freq_hz * t)).astype(np.float32)
    return samples


def test_ring_buffer_tracks_absolute_mic_sample_indices() -> None:
    async def run() -> None:
        ring = RingBuffer(capacity_sec=1.0)
        first = (np.arange(4800, dtype=np.int16)).tobytes()
        second = (np.arange(4800, 9600, dtype=np.int16)).tobytes()
        await ring.append(first, end_time=10.0)
        await ring.append(second, end_time=10.1)
        window = await ring.read_window_with_index(9.95, 10.05)
        assert 2399 <= window.start_index <= 2400
        assert 4799 <= len(window.samples) <= 4802

    asyncio.run(run())


def test_onset_detector_reports_burst_start_not_peak_window_center() -> None:
    burst_start = 9_600
    burst_duration = int(0.100 * SAMPLE_RATE)
    samples = _tone(burst_start, burst_duration, total=24_000)

    onset = EnvelopeDetector.detect_onset_in_samples(
        samples,
        base_time=0.0,
        base_sample_index=100_000,
        noise_floor_db=-90.0,
    )
    peak = EnvelopeDetector.detect_peak_in_samples(
        samples,
        base_time=0.0,
        base_sample_index=100_000,
        noise_floor_db=-90.0,
    )

    assert onset is not None
    assert peak is not None
    assert abs(onset["arrival_sample_index"] - (100_000 + burst_start)) <= 480
    assert peak["arrival_sample_index"] - onset["arrival_sample_index"] >= int(0.020 * SAMPLE_RATE)


def test_pattern_detector_matches_emit_spacing_in_mic_sample_indices() -> None:
    first = 12_000
    offsets = [0, 12_000, 27_000]
    duration = int(0.030 * SAMPLE_RATE)
    total = first + offsets[-1] + duration + 10_000
    samples = np.zeros(total, dtype=np.float32)
    for offset in offsets:
        samples += _tone(first + offset, duration, total)

    base_sample = 500_000
    emit_frames = [1_000_000 + offset for offset in offsets]
    match = EnvelopeDetector.detect_pattern_in_samples(
        samples,
        base_time=0.0,
        base_sample_index=base_sample,
        noise_floor_db=-90.0,
        emit_frame_indices=emit_frames,
        tolerance_ms=5.0,
    )

    assert match is not None
    assert match["detector_mode"] == "pattern"
    observed = match["matched_arrival_sample_indices"]
    assert [sample - observed[0] for sample in observed] == offsets
    assert max(abs(error) for error in match["matched_error_ms"]) <= 0.001


def test_pattern_detector_uses_clock_prior_to_avoid_late_echo_group() -> None:
    first = 12_000
    emit_offsets = [0, 14_336, 28_672]
    true_offsets = [0, 14_456, 28_552]
    echo_offset = 2_168
    duration = int(0.006 * SAMPLE_RATE)
    total = first + emit_offsets[-1] + echo_offset + duration + 8_000
    samples = np.zeros(total, dtype=np.float32)
    for offset in true_offsets:
        samples += _tone(first + offset, duration, total)
    for offset in emit_offsets:
        samples += _tone(first + echo_offset + offset, duration, total)

    base_sample = 500_000
    emit_frames = [1_000_000 + offset for offset in emit_offsets]
    expected_delta = base_sample + first - emit_frames[0]

    unprioritized = EnvelopeDetector.detect_pattern_in_samples(
        samples,
        base_time=0.0,
        base_sample_index=base_sample,
        noise_floor_db=-90.0,
        emit_frame_indices=emit_frames,
        tolerance_ms=5.0,
    )
    prioritized = EnvelopeDetector.detect_pattern_in_samples(
        samples,
        base_time=0.0,
        base_sample_index=base_sample,
        noise_floor_db=-90.0,
        emit_frame_indices=emit_frames,
        tolerance_ms=5.0,
        expected_delta_samples=expected_delta,
        clock_tolerance_ms=20.0,
    )

    assert unprioritized is not None
    assert prioritized is not None
    assert unprioritized["arrival_sample_index"] - prioritized["arrival_sample_index"] > 1_400
    assert abs(prioritized["arrival_sample_index"] - (base_sample + first)) <= 480
    assert prioritized["pattern_selection_reason"] == "clock_prior"
    assert abs(prioritized["pattern_clock_prior_error_ms"]) < 10.0


def test_pattern_detector_rejects_groups_that_jump_outside_clock_prior() -> None:
    first = 12_000
    offsets = [0, 14_336, 28_672]
    late_jump = 2_400
    duration = int(0.006 * SAMPLE_RATE)
    total = first + offsets[-1] + late_jump + duration + 8_000
    samples = np.zeros(total, dtype=np.float32)
    for offset in offsets:
        samples += _tone(first + late_jump + offset, duration, total)

    base_sample = 500_000
    emit_frames = [1_000_000 + offset for offset in offsets]
    expected_delta = base_sample + first - emit_frames[0]

    analysis = EnvelopeDetector.analyze_pattern_in_samples(
        samples,
        base_time=0.0,
        base_sample_index=base_sample,
        noise_floor_db=-90.0,
        emit_frame_indices=emit_frames,
        tolerance_ms=5.0,
        expected_delta_samples=expected_delta,
        clock_tolerance_ms=20.0,
    )

    assert analysis["reject_reason"] == "clock_prior_mismatch"
    assert analysis["pattern_match_count"] == 1
    assert analysis["pattern_rejected_by_clock_count"] == 1
    assert analysis["best_unprioritized_pattern_mean_abs_error_ms"] < 2.0
    assert analysis["best_unprioritized_pattern_clock_prior_error_ms"] > 35.0
    assert "selected" not in analysis


def test_pattern_detector_has_independent_candidate_snr_floor() -> None:
    first = 12_000
    offsets = [0, 14_336, 28_672]
    duration = int(0.030 * SAMPLE_RATE)
    total = first + offsets[-1] + duration + 8_000
    samples = np.zeros(total, dtype=np.float32)
    for offset in offsets:
        samples += _tone(first + offset, duration, total)

    onset_window = int(0.010 * SAMPLE_RATE)
    onset_power = EnvelopeDetector._band_power_db(samples[first : first + onset_window])
    noise_floor = onset_power - 10.5
    base_sample = 500_000
    emit_frames = [1_000_000 + offset for offset in offsets]

    strict = EnvelopeDetector.detect_pattern_in_samples(
        samples,
        base_time=0.0,
        base_sample_index=base_sample,
        noise_floor_db=noise_floor,
        emit_frame_indices=emit_frames,
        tolerance_ms=5.0,
        min_snr_db=12.0,
    )
    pattern_floor = EnvelopeDetector.detect_pattern_in_samples(
        samples,
        base_time=0.0,
        base_sample_index=base_sample,
        noise_floor_db=noise_floor,
        emit_frame_indices=emit_frames,
        tolerance_ms=5.0,
        min_snr_db=9.0,
    )

    assert strict is None
    assert pattern_floor is not None
    assert pattern_floor["pattern_min_snr_db"] == 9.0
    assert pattern_floor["snr_db"] >= 9.0
