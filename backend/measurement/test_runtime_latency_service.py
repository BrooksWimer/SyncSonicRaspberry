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
