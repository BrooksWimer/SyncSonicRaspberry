from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from pathlib import Path

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

sys.modules.setdefault("dbus", types.SimpleNamespace(SystemBus=lambda: None))

from measurement.runtime_latency_service import (  # noqa: E402
    CLOCK_PRIOR_RESET_CYCLES,
    CONFIDENCE_WINDOW_N,
    DYNAMIC_TARGET_BASELINE_N,
    EnvelopeDetector,
    FAST_ALIGN_CONFIDENCE_WINDOW_N,
    RingBuffer,
    RuntimeSyncService,
    SAMPLE_RATE,
    SpeakerTarget,
    ActuationResult,
    _build_parser,
    read_ultrasonic_exclusions,
    discover_active_speakers,
)
from measurement.calibration_targets import (  # noqa: E402
    read_startup_tune_target,
    record_startup_tune_target,
)


def _tone(start: int, duration: int, total: int, freq_hz: float = 18_500.0) -> np.ndarray:
    samples = np.zeros(total, dtype=np.float32)
    t = np.arange(duration, dtype=np.float64) / SAMPLE_RATE
    samples[start : start + duration] = (0.8 * np.sin(2.0 * np.pi * freq_hz * t)).astype(np.float32)
    return samples


def test_startup_tune_target_missing_file_falls_back_to_cli_default(tmp_path: Path) -> None:
    path = tmp_path / "startup_tune_targets.json"

    resolved = read_startup_tune_target("AA:BB:CC:DD:EE:FF", 500.0, path=path)

    assert resolved.target_total_ms == 500.0
    assert resolved.source == "cli_default_missing_file"


def test_startup_tune_target_reads_shared_persistent_value_before_cli_default(tmp_path: Path) -> None:
    path = tmp_path / "startup_tune_targets.json"
    record_startup_tune_target(612.5, path=path)

    resolved = read_startup_tune_target("AA:BB:CC:DD:EE:FF", 500.0, path=path)

    assert resolved.target_total_ms == 612.5
    assert resolved.source == "shared"


def test_startup_tune_target_uses_per_speaker_before_shared(tmp_path: Path) -> None:
    path = tmp_path / "startup_tune_targets.json"
    record_startup_tune_target(612.5, path=path)
    record_startup_tune_target(455.25, mac="aa:bb:cc:dd:ee:ff", path=path)

    resolved = read_startup_tune_target("AA:BB:CC:DD:EE:FF", 500.0, path=path)

    assert resolved.target_total_ms == 455.25
    assert resolved.source == "per_speaker"


def test_startup_tune_target_persists_shared_and_per_speaker_values(tmp_path: Path) -> None:
    path = tmp_path / "startup_tune_targets.json"

    record_startup_tune_target(700.0, path=path)
    record_startup_tune_target(480.0, mac="11:22:33:44:55:66", path=path)

    assert read_startup_tune_target("AA:BB:CC:DD:EE:FF", 500.0, path=path).target_total_ms == 700.0
    assert read_startup_tune_target("11:22:33:44:55:66", 500.0, path=path).target_total_ms == 480.0


def test_ultrasonic_exclusion_file_is_read_case_insensitively(tmp_path: Path) -> None:
    path = tmp_path / "ultrasonic_excluded.json"
    path.write_text('{"excluded_macs":["aa:bb:cc:dd:ee:ff"]}\n', encoding="utf-8")

    assert read_ultrasonic_exclusions(path) == {"AA:BB:CC:DD:EE:FF"}


def test_discovery_applies_ultrasonic_exclusions_before_limit(monkeypatch) -> None:
    sockets = {
        "AA:BB:CC:DD:EE:01": Path("/tmp/a.sock"),
        "AA:BB:CC:DD:EE:02": Path("/tmp/b.sock"),
    }
    monkeypatch.setattr("measurement.runtime_latency_service._scan_filter_sockets", lambda: sockets)
    monkeypatch.setattr(
        "measurement.runtime_latency_service._connected_speaker_macs",
        lambda: set(sockets),
    )

    targets = discover_active_speakers(
        limit=1,
        excluded_macs={"AA:BB:CC:DD:EE:01"},
    )

    assert [target.mac for target in targets] == ["AA:BB:CC:DD:EE:02"]


def test_runtime_service_exits_cleanly_when_no_speakers_connected_after_timeout(monkeypatch) -> None:
    async def run() -> None:
        args = _build_parser().parse_args(
            [
                "--startup-gate-attempts",
                "2",
                "--startup-gate-interval-sec",
                "0",
            ]
        )
        service = RuntimeSyncService(args)
        started_capture = False

        async def fake_capture_start() -> None:
            nonlocal started_capture
            started_capture = True

        monkeypatch.setattr(
            "measurement.runtime_latency_service.discover_active_speakers",
            lambda limit: [],
        )
        monkeypatch.setattr(service.capture, "start", fake_capture_start)

        await service.run()

        assert started_capture is False
        assert service.loop_task is None
        assert service.state.targets == []

    asyncio.run(run())


def test_dynamic_target_baseline_excludes_existing_filter_delay(monkeypatch) -> None:
    args = _build_parser().parse_args(["--detector-mode", "pattern", "--target-total-ms", "500"])
    service = RuntimeSyncService(args)
    target = SpeakerTarget(mac="AA:BB:CC:DD:EE:FF", socket_path=Path("/tmp/filter.sock"))
    service.state.targets = [target]

    applied: list[tuple[float, float, float]] = []

    class FakeActuator:
        def apply(
            self,
            _mac: str,
            measured_latency_ms: float,
            target_total_ms: float,
            current_filter_delay_ms: float,
            **_kwargs,
        ) -> ActuationResult:
            applied.append((measured_latency_ms, target_total_ms, current_filter_delay_ms))
            return ActuationResult(action="baseline")

    monkeypatch.setattr(
        "measurement.calibration_targets.record_startup_tune_target",
        lambda _target_total_ms: None,
    )
    service.slice5_actuator = FakeActuator()  # type: ignore[assignment]

    for _ in range(DYNAMIC_TARGET_BASELINE_N - 1):
        assert service._apply_slice5_proposal(
            target,
            current_filter_delay_ms=4534.0,
            measured_latency_ms=5022.0,
        ) is None

    result = service._apply_slice5_proposal(
        target,
        current_filter_delay_ms=4534.0,
        measured_latency_ms=5022.0,
    )

    assert result is not None
    assert service.args.target_total_ms == 538.0
    assert applied == [(5022.0, 538.0, 4534.0)]


def test_persisted_target_skips_baseline_wait(monkeypatch) -> None:
    args = _build_parser().parse_args(["--detector-mode", "pattern", "--target-total-ms", "500"])
    service = RuntimeSyncService(args)
    target = SpeakerTarget(mac="AA:BB:CC:DD:EE:FF", socket_path=Path("/tmp/filter.sock"))
    service.state.targets = [target]

    applied: list[tuple[float, float, float]] = []

    class FakeActuator:
        def apply(
            self,
            _mac: str,
            measured_latency_ms: float,
            target_total_ms: float,
            current_filter_delay_ms: float,
            **_kwargs,
        ) -> ActuationResult:
            applied.append((measured_latency_ms, target_total_ms, current_filter_delay_ms))
            return ActuationResult(action="corrected")

    monkeypatch.setattr(
        "measurement.runtime_latency_service.read_startup_tune_target",
        lambda _mac, _target_total_ms: types.SimpleNamespace(
            target_total_ms=538.0,
            source="shared",
            path=Path("/tmp/startup_tune_targets.json"),
        ),
    )
    service.slice5_actuator = FakeActuator()  # type: ignore[assignment]

    result = service._apply_slice5_proposal(
        target,
        current_filter_delay_ms=4534.0,
        measured_latency_ms=5022.0,
    )

    assert result is not None
    assert service._target_from_persistence is True
    assert service.baseline_samples[target.mac] == [488.0]
    assert applied == [(5022.0, 538.0, 4534.0)]


def test_cold_start_waits_for_baseline(monkeypatch) -> None:
    args = _build_parser().parse_args(["--detector-mode", "pattern", "--target-total-ms", "500"])
    service = RuntimeSyncService(args)
    target = SpeakerTarget(mac="AA:BB:CC:DD:EE:FF", socket_path=Path("/tmp/filter.sock"))
    service.state.targets = [target]

    applied: list[tuple[float, float, float]] = []

    class FakeActuator:
        def apply(
            self,
            _mac: str,
            measured_latency_ms: float,
            target_total_ms: float,
            current_filter_delay_ms: float,
            **_kwargs,
        ) -> ActuationResult:
            applied.append((measured_latency_ms, target_total_ms, current_filter_delay_ms))
            return ActuationResult(action="baseline")

    monkeypatch.setattr(
        "measurement.runtime_latency_service.read_startup_tune_target",
        lambda _mac, target_total_ms: types.SimpleNamespace(
            target_total_ms=target_total_ms,
            source="cli_default_missing_file",
            path=Path("/tmp/startup_tune_targets.json"),
        ),
    )
    monkeypatch.setattr(
        "measurement.calibration_targets.record_startup_tune_target",
        lambda _target_total_ms: None,
    )
    service.slice5_actuator = FakeActuator()  # type: ignore[assignment]

    results = [
        service._apply_slice5_proposal(
            target,
            current_filter_delay_ms=4534.0,
            measured_latency_ms=5022.0,
        )
        for _ in range(DYNAMIC_TARGET_BASELINE_N)
    ]

    assert results[0] is None
    assert results[1] is None
    assert results[2] is not None
    assert service._target_from_persistence is False
    assert service.args.target_total_ms == 538.0
    assert applied == [(5022.0, 538.0, 4534.0)]


def test_fast_align_uses_smaller_window_n(monkeypatch) -> None:
    args = _build_parser().parse_args(["--detector-mode", "pattern", "--target-total-ms", "370"])
    service = RuntimeSyncService(args)
    target = SpeakerTarget(mac="AA:BB:CC:DD:EE:FF", socket_path=Path("/tmp/filter.sock"))
    service.state.targets = [target]
    service.baseline_samples[target.mac] = [370.0] * DYNAMIC_TARGET_BASELINE_N
    captured_window_n: list[int] = []

    class FakeActuator:
        def apply(
            self,
            _mac: str,
            _measured_latency_ms: float,
            _target_total_ms: float,
            _current_filter_delay_ms: float,
            *,
            confidence_window_n: int = CONFIDENCE_WINDOW_N,
        ) -> ActuationResult:
            captured_window_n.append(confidence_window_n)
            return ActuationResult(action="corrected")

    monkeypatch.setattr(
        "measurement.runtime_latency_service.read_startup_tune_target",
        lambda _mac, target_total_ms: types.SimpleNamespace(
            target_total_ms=target_total_ms,
            source="test",
            path=Path("/tmp/startup_tune_targets.json"),
        ),
    )
    service.slice5_actuator = FakeActuator()  # type: ignore[assignment]

    service._fast_align_active = False
    service._apply_slice5_proposal(
        target,
        current_filter_delay_ms=0.0,
        measured_latency_ms=430.0,
    )
    service._fast_align_active = True
    service._apply_slice5_proposal(
        target,
        current_filter_delay_ms=0.0,
        measured_latency_ms=430.0,
    )

    assert captured_window_n == [CONFIDENCE_WINDOW_N, FAST_ALIGN_CONFIDENCE_WINDOW_N]


def test_fast_align_entry_preserves_amp_index(monkeypatch, tmp_path: Path) -> None:
    args = _build_parser().parse_args(["--detector-mode", "pattern"])
    service = RuntimeSyncService(args)
    target = SpeakerTarget(mac="AA:BB:CC:DD:EE:FF", socket_path=Path("/tmp/filter.sock"))
    service._sync_slice5_actuator([target])
    assert service.slice5_actuator is not None
    service.slice5_actuator.burst_amp_indices[target.mac] = 2

    trigger_path = tmp_path / "silent_align_requested"
    trigger_path.touch()
    monkeypatch.setattr("measurement.runtime_latency_service.FAST_ALIGN_TRIGGER_PATH", trigger_path)

    assert service._refresh_fast_align_state() is True
    assert service.slice5_actuator.burst_amp_indices[target.mac] == 2

    joining = SpeakerTarget(mac="11:22:33:44:55:66", socket_path=Path("/tmp/filter2.sock"))
    service._sync_slice5_actuator([target, joining])

    assert service.slice5_actuator.burst_amp_indices[target.mac] == 2
    assert service.slice5_actuator.burst_amp_indices[joining.mac] == 0


def test_fast_align_bounded_exit_writes_silent_align_complete(monkeypatch, tmp_path: Path) -> None:
    args = _build_parser().parse_args(["--detector-mode", "pattern"])
    service = RuntimeSyncService(args)
    target = SpeakerTarget(mac="AA:BB:CC:DD:EE:FF", socket_path=Path("/tmp/filter.sock"))
    service.state.targets = [target]
    service._fast_align_active = True
    service._fast_align_start_monotonic = time.monotonic()
    service._fast_align_converged_cycles = 0

    corrections_path = tmp_path / "runtime_corrections.jsonl"
    monkeypatch.setattr(
        "measurement.runtime_latency_service.RUNTIME_CORRECTIONS_PATH",
        corrections_path,
    )

    # One converged cycle is not enough to exit the bounded fast pass.
    service._maybe_complete_fast_align(["within_threshold"])
    assert service._fast_align_active is True
    assert not corrections_path.exists()

    # A cycle that still needed a correction resets the converged counter.
    service._maybe_complete_fast_align(["corrected"])
    assert service._fast_align_active is True

    # Two consecutive converged cycles end fast-align and write the event.
    service._maybe_complete_fast_align(["within_threshold"])
    service._maybe_complete_fast_align(["within_threshold"])
    assert service._fast_align_active is False
    lines = corrections_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["phase"] == "silent_align_complete"
    assert event["speaker_macs"] == ["AA:BB:CC:DD:EE:FF"]


def test_applied_delay_correction_resets_sample_clock_prior_window(monkeypatch) -> None:
    async def run() -> None:
        args = _build_parser().parse_args(["--detector-mode", "pattern", "--warmup-sec", "0"])
        service = RuntimeSyncService(args)
        target = SpeakerTarget(mac="AA:BB:CC:DD:EE:FF", socket_path=Path("/tmp/filter.sock"))
        target.last_sample_clock_delta_samples = 12_345.0
        target.sample_clock_baseline_samples = 12_345.0

        send_calls: list[tuple[Path, str]] = []

        def fake_send(socket_path: Path, payload: str):
            send_calls.append((socket_path, payload))
            if payload == "query":
                return {"target_delay_samples": 0, "current_delay_samples": 0}
            if payload == "query_emit_timestamps":
                return {
                    "entries": [
                        {"frame_index": 1000},
                        {"frame_index": 2000},
                        {"frame_index": 3000},
                    ],
                }
            if payload.startswith("emit_burst"):
                return {"ok": True}
            return {"ok": True}

        async def fake_sleep(_delay: float) -> None:
            return None

        async def fake_analyze_pattern(*_args, **_kwargs):
            return {
                "selected": {
                    "arrival_monotonic": 100.0,
                    "arrival_sample_index": 14_000,
                    "sample_clock_anchor_sample_index": 14_000,
                    "sample_clock_anchor_monotonic": 100.0,
                    "clock_delta_samples": 13_000.0,
                    "peak_power_db": -10.0,
                    "noise_floor_db": -40.0,
                    "snr_db": 30.0,
                    "detector_mode": "pattern",
                    "candidate_count": 3,
                    "matched_arrival_sample_indices": [14_000, 15_000, 16_000],
                    "matched_error_ms": [0.0, 0.0, 0.0],
                    "pattern_mean_abs_error_ms": 0.0,
                    "pattern_max_abs_error_ms": 0.0,
                    "pattern_min_snr_db": 9.0,
                    "pattern_landmark": "envelope",
                    "pattern_carrier_hz": 18_500.0,
                    "pattern_clock_delta_spread_ms": 0.0,
                    "pattern_selection_reason": "best_spacing",
                    "pattern_match_count": 1,
                    "pattern_rejected_by_clock_count": 0,
                }
            }

        monkeypatch.setattr("measurement.runtime_latency_service._send_filter_command", fake_send)
        monkeypatch.setattr("measurement.runtime_latency_service.asyncio.sleep", fake_sleep)
        monkeypatch.setattr(service.detector, "analyze_pattern", fake_analyze_pattern)
        monkeypatch.setattr(
            service,
            "_apply_slice5_proposal",
            lambda *_args, **_kwargs: ActuationResult(
                action="corrected",
                delta_ms=60.0,
                clock_prior_reset=True,
            ),
        )

        await service._measure_pattern(target)

        assert target.last_sample_clock_delta_samples is None
        assert target.clock_prior_reset_remaining == CLOCK_PRIOR_RESET_CYCLES

    asyncio.run(run())


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
    observed_offsets = [sample - observed[0] for sample in observed]
    assert max(abs(observed - expected) for observed, expected in zip(observed_offsets, offsets)) <= 24
    assert max(abs(error) for error in match["matched_error_ms"]) <= 0.5
    assert match["pattern_landmark"] == "envelope"


def test_pattern_detector_uses_clock_prior_to_avoid_late_echo_group() -> None:
    first = 12_000
    emit_offsets = [0, 14_336, 28_672]
    true_offsets = [0, 14_398, 28_552]
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
    t = np.arange(total, dtype=np.float64) / SAMPLE_RATE
    samples = (0.20 * np.sin(2.0 * np.pi * 18_500.0 * t)).astype(np.float32)
    for offset in offsets:
        samples += _tone(first + offset, duration, total)

    base_sample = 500_000
    emit_frames = [1_000_000 + offset for offset in offsets]

    strict = EnvelopeDetector.detect_pattern_in_samples(
        samples,
        base_time=0.0,
        base_sample_index=base_sample,
        noise_floor_db=-90.0,
        emit_frame_indices=emit_frames,
        tolerance_ms=5.0,
        min_snr_db=12.0,
    )
    pattern_floor = EnvelopeDetector.detect_pattern_in_samples(
        samples,
        base_time=0.0,
        base_sample_index=base_sample,
        noise_floor_db=-90.0,
        emit_frame_indices=emit_frames,
        tolerance_ms=5.0,
        min_snr_db=9.0,
    )

    assert strict is None
    assert pattern_floor is not None
    assert pattern_floor["pattern_min_snr_db"] == 9.0
    assert pattern_floor["snr_db"] >= 9.0


def test_demodulated_envelope_pattern_uses_leading_edge_not_loudest_window() -> None:
    first = 12_000
    offsets = [0, 14_336, 28_672]
    duration = int(0.050 * SAMPLE_RATE)
    bump_offset = int(0.025 * SAMPLE_RATE)
    bump_duration = int(0.010 * SAMPLE_RATE)
    total = first + offsets[-1] + duration + 8_000
    samples = np.zeros(total, dtype=np.float32)
    for offset in offsets:
        samples += _tone(first + offset, duration, total, freq_hz=18_500.0) * 0.25
        samples += _tone(first + offset + bump_offset, bump_duration, total, freq_hz=18_500.0) * 0.75

    base_sample = 500_000
    emit_frames = [1_000_000 + offset for offset in offsets]
    match = EnvelopeDetector.detect_pattern_in_samples(
        samples,
        base_time=0.0,
        base_sample_index=base_sample,
        noise_floor_db=-90.0,
        emit_frame_indices=emit_frames,
        tolerance_ms=5.0,
        min_snr_db=9.0,
    )
    peak = EnvelopeDetector.detect_peak_in_samples(
        samples[first - 2_000 : first + 5_000],
        base_time=0.0,
        base_sample_index=base_sample + first - 2_000,
        noise_floor_db=-90.0,
    )

    assert match is not None
    assert peak is not None
    assert abs(match["arrival_sample_index"] - (base_sample + first)) <= 480
    assert peak["arrival_sample_index"] - match["arrival_sample_index"] >= int(0.020 * SAMPLE_RATE)
