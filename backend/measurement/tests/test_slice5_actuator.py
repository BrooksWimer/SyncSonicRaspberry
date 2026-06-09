from __future__ import annotations

import json
import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from measurement.slice5_actuator import (  # noqa: E402
    BURST_AMP_LADDER_X1000,
    CONFIDENCE_WINDOW_N,
    SpeakerActuator,
)


MAC = "AA:BB:CC:DD:EE:FF"


def _writer(calls: list[tuple[str, str]]):
    def write(path: Path, command: str):
        calls.append((str(path), command))
        return {"ok": True}

    return write


def _actuator(
    calls: list[tuple[str, str]],
    *,
    mac: str = MAC,
    runtime_corrections_path: Path | str | None = None,
) -> SpeakerActuator:
    return SpeakerActuator(
        {mac: Path(f"/tmp/{mac.replace(':', '_')}.sock")},
        socket_writer=_writer(calls),
        runtime_corrections_path=runtime_corrections_path,
    )


def _establish_baseline(actuator: SpeakerActuator) -> None:
    result = actuator.apply(MAC, 370.0, 370.0, 0.0)
    assert result.action == "baseline"


def _apply_confidence_window(
    actuator: SpeakerActuator,
    measured_latency_ms: float,
    target_total_ms: float,
    current_filter_delay: float,
) -> list[str]:
    return [
        actuator.apply(
            MAC,
            measured_latency_ms,
            target_total_ms,
            current_filter_delay,
        ).action
        for _idx in range(CONFIDENCE_WINDOW_N)
    ]


def test_first_valid_burst_establishes_baseline_and_does_not_correct() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)

    result = actuator.apply(MAC, 370.0, 370.0, 0.0)

    assert result.action == "baseline"
    assert result.clock_prior_reset is False
    assert actuator.baseline_established[MAC] is True
    assert calls == []


def test_subsequent_burst_within_threshold_does_not_correct() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    actions = _apply_confidence_window(actuator, 370.75, 370.0, 20.0)

    assert actions == ["building_window"] * (CONFIDENCE_WINDOW_N - 1) + ["within_threshold"]
    assert calls == []


def test_subsequent_burst_above_threshold_applies_set_delay_with_correct_sign() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    actions = [
        actuator.apply(MAC, 430.0, 370.0, 100.0)
        for _idx in range(CONFIDENCE_WINDOW_N)
    ]
    result = actions[-1]

    assert result.action == "corrected"
    assert result.delta_ms == 60.0
    assert result.clock_prior_reset is True
    assert [item.action for item in actions] == ["building_window"] * (CONFIDENCE_WINDOW_N - 1) + ["corrected"]
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 40.000")]


def test_apply_with_negative_offset_increases_delay() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    actions = [
        actuator.apply(MAC, 310.0, 370.0, 100.0)
        for _idx in range(CONFIDENCE_WINDOW_N)
    ]
    result = actions[-1]

    assert result.action == "corrected"
    assert result.delta_ms == -60.0
    assert result.clock_prior_reset is True
    assert [item.action for item in actions] == ["building_window"] * (CONFIDENCE_WINDOW_N - 1) + ["corrected"]
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 160.000")]


def test_large_consistent_initial_offset_confidence_corrects_once() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    actions = [
        actuator.apply(MAC, 970.0, 370.0, 800.0)
        for _idx in range(CONFIDENCE_WINDOW_N)
    ]
    result = actions[-1]

    assert [item.action for item in actions] == ["building_window"] * (CONFIDENCE_WINDOW_N - 1) + ["corrected"]
    assert result.delta_ms == 600.0
    assert result.clock_prior_reset is True
    assert actuator.baseline_established[MAC] is True
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 200.000")]


def test_disagreeing_high_std_window_does_not_correct_large_offset() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    values = [970.0, 370.0, 970.0, 370.0, 970.0]
    actions = [
        actuator.apply(MAC, measured, 370.0, 800.0)
        for measured in values
    ]

    # Key property: no correction must fire on a disagreeing window.
    # The first CONFIDENCE_WINDOW_N-1 are building; thereafter the window
    # alternates between insufficient_confidence (high-value majority) and
    # within_threshold (low-value majority), but never "corrected".
    assert "corrected" not in [item.action for item in actions]
    assert [item.action for item in actions[:CONFIDENCE_WINDOW_N - 1]] == ["building_window"] * (CONFIDENCE_WINDOW_N - 1)
    assert actions[-1].clock_prior_reset is False
    assert calls == []


def test_missed_burst_does_nothing() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)

    result = actuator.apply(MAC, None, None, 20.0, missed_burst=True)

    assert result.action == "missed"
    assert result.clock_prior_reset is False
    assert actuator.baseline_established[MAC] is False
    assert calls == []


def test_burst_amp_escalates_after_misses_and_de_escalates_on_success() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)

    assert BURST_AMP_LADDER_X1000 == (300, 600, 950)
    assert actuator.burst_amp_x1000_for(MAC) == 300

    actuator.apply(MAC, None, None, 20.0, missed_burst=True)
    assert actuator.burst_amp_x1000_for(MAC) == 600

    actuator.apply(MAC, 370.0, 370.0, 20.0)
    assert actuator.consecutive_missed_bursts[MAC] == 0
    assert actuator.burst_amp_x1000_for(MAC) == 300

    actuator.apply(MAC, None, None, 20.0, missed_burst=True)
    actuator.apply(MAC, None, None, 20.0, missed_burst=True)
    assert actuator.burst_amp_x1000_for(MAC) == 950

    actuator.apply(MAC, 370.0, 370.0, 20.0)
    assert actuator.burst_amp_x1000_for(MAC) == 600
    assert calls == []


def test_burst_amp_ladder_is_per_speaker() -> None:
    other = "11:22:33:44:55:66"
    calls: list[tuple[str, str]] = []
    actuator = SpeakerActuator(
        {
            MAC: Path("/tmp/a.sock"),
            other: Path("/tmp/b.sock"),
        },
        socket_writer=_writer(calls),
    )

    actuator.apply(MAC, None, None, 20.0, missed_burst=True)

    assert actuator.burst_amp_x1000_for(MAC) == 600
    assert actuator.burst_amp_x1000_for(other) == 300
    assert calls == []


def test_speaker_disconnected_event_resets_baseline() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    actuator.sync_sockets({})

    assert actuator.baseline_established[MAC] is False
    assert calls == []


def test_emergency_stop_zeros_ppm_and_resets_baseline() -> None:
    calls: list[tuple[str, str]] = []
    actuator = SpeakerActuator(
        {
            MAC: Path("/tmp/a.sock"),
            "11:22:33:44:55:66": Path("/tmp/b.sock"),
        },
        socket_writer=_writer(calls),
    )
    actuator.apply(MAC, 370.0, 370.0, 0.0)

    actuator.emergency_stop()

    assert calls == [
        ("/tmp/b.sock", "set_rate_ppm 0"),
        ("/tmp/a.sock", "set_rate_ppm 0"),
    ]
    assert actuator.baseline_established[MAC] is False
    assert actuator.baseline_established["11:22:33:44:55:66"] is False


def test_slider_aware_clock_prior_reset_cycles_still_emitted() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    actions = [
        actuator.apply(MAC, 310.0, 370.0, 100.0)
        for _idx in range(CONFIDENCE_WINDOW_N)
    ]
    result = actions[-1]

    assert result.action == "corrected"
    assert result.delta_ms == -60.0
    assert result.clock_prior_reset is True
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 160.000")]

def test_startup_tune_convergence_with_fixed_target() -> None:
    # Matches the startup-tune semantic verified empirically: target_total_ms is a fixed
    # reference value; new_filter_delay = current_filter_delay + (target - measured).
    # A speaker measuring 510ms against a 500ms target with current_filter_delay 100ms
    # should be told to subtract 10ms of filter delay so its NEXT cycle measures closer
    # to 500ms.
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    actions = [
        actuator.apply(MAC, 560.0, 500.0, 100.0)
        for _idx in range(CONFIDENCE_WINDOW_N)
    ]
    result = actions[-1]
    assert result.action == "corrected"
    assert result.delta_ms == 60.0
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 40.000")]

    # And the converse: a fast speaker at 490ms vs target 500ms gets MORE filter delay.
    calls.clear()
    actuator2 = _actuator(calls)
    _establish_baseline(actuator2)
    actions2 = [
        actuator2.apply(MAC, 440.0, 500.0, 100.0)
        for _idx in range(CONFIDENCE_WINDOW_N)
    ]
    result2 = actions2[-1]
    assert result2.action == "corrected"
    assert result2.delta_ms == -60.0
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 160.000")]


def test_corrected_action_appends_runtime_correction_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "runtime_corrections.jsonl"
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls, runtime_corrections_path=path)
    _establish_baseline(actuator)

    actions = [
        actuator.apply(MAC, 430.0, 370.0, 100.0)
        for _idx in range(CONFIDENCE_WINDOW_N)
    ]
    result = actions[-1]

    assert result.action == "corrected"
    # The JSONL file may contain multiple events (e.g. slice5_actuation log lines);
    # find the canonical runtime_correction event that the watcher forwards to BLE.
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    correction_events = [e for e in events if e.get("event") == "runtime_correction"]
    assert correction_events, "No runtime_correction event found in JSONL"
    event = correction_events[-1]
    assert event["action"] == "corrected"
    assert event["event"] == "runtime_correction"
    assert event["mac"] == MAC
    assert event["measured_latency_ms"] == 430.0
    assert event["target_total_ms"] == 370.0
    assert event["current_filter_delay_ms"] == 100.0
    assert event["delta_ms"] == 60.0
    assert event["new_filter_delay_ms"] == 40.0
