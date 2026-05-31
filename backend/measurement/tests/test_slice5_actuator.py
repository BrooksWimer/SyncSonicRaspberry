from __future__ import annotations

import json
import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from measurement.slice5_actuator import BURST_AMP_LADDER_X1000, SpeakerActuator  # noqa: E402


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

    result = actuator.apply(MAC, 370.75, 370.0, 20.0)

    assert result.action == "within_threshold"
    assert result.clock_prior_reset is False
    assert calls == []


def test_subsequent_burst_above_threshold_applies_set_delay_with_correct_sign() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    result = actuator.apply(MAC, 377.0, 370.0, 20.0)

    assert result.action == "corrected"
    assert result.delta_ms == 7.0
    assert result.clock_prior_reset is True
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 13.000")]


def test_apply_with_negative_offset_increases_delay() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    result = actuator.apply(MAC, 363.0, 370.0, 20.0)

    assert result.action == "corrected"
    assert result.delta_ms == -7.0
    assert result.clock_prior_reset is True
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 27.000")]


def test_freak_outlier_skips_without_corrupting_baseline() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    result = actuator.apply(MAC, 900.0, 370.0, 20.0)

    assert result.action == "freak_skip"
    assert result.clock_prior_reset is False
    assert actuator.baseline_established[MAC] is True
    assert calls == []


def test_missed_burst_does_nothing() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)

    result = actuator.apply(MAC, None, None, 20.0, missed_burst=True)

    assert result.action == "missed"
    assert result.clock_prior_reset is False
    assert actuator.baseline_established[MAC] is False
    assert calls == []


def test_burst_amp_escalates_after_three_consecutive_misses_and_sticks() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)

    assert BURST_AMP_LADDER_X1000 == (300, 600, 950)
    assert actuator.burst_amp_x1000_for(MAC) == 300

    for _idx in range(2):
        result = actuator.apply(MAC, None, None, 20.0, missed_burst=True)
        assert result.action == "missed"
        assert actuator.burst_amp_x1000_for(MAC) == 300

    actuator.apply(MAC, None, None, 20.0, missed_burst=True)
    assert actuator.burst_amp_x1000_for(MAC) == 600

    actuator.apply(MAC, 370.0, 370.0, 20.0)
    assert actuator.consecutive_missed_bursts[MAC] == 0
    assert actuator.burst_amp_x1000_for(MAC) == 600

    for _idx in range(3):
        actuator.apply(MAC, None, None, 20.0, missed_burst=True)
    assert actuator.burst_amp_x1000_for(MAC) == 950

    actuator.apply(MAC, 370.0, 370.0, 20.0)
    assert actuator.burst_amp_x1000_for(MAC) == 950
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

    for _idx in range(3):
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

    result = actuator.apply(MAC, 365.0, 370.0, 20.0)

    assert result.action == "corrected"
    assert result.delta_ms == -5.0
    assert result.clock_prior_reset is True
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 25.000")]

def test_startup_tune_convergence_with_fixed_target() -> None:
    # Matches the startup-tune semantic verified empirically: target_total_ms is a fixed
    # reference value; new_filter_delay = current_filter_delay + (target - measured).
    # A speaker measuring 510ms against a 500ms target with current_filter_delay 100ms
    # should be told to subtract 10ms of filter delay so its NEXT cycle measures closer
    # to 500ms.
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    result = actuator.apply(MAC, 510.0, 500.0, 100.0)
    assert result.action == "corrected"
    assert result.delta_ms == 10.0
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 90.000")]

    # And the converse: a fast speaker at 490ms vs target 500ms gets MORE filter delay.
    calls.clear()
    actuator2 = _actuator(calls)
    _establish_baseline(actuator2)
    result2 = actuator2.apply(MAC, 490.0, 500.0, 100.0)
    assert result2.action == "corrected"
    assert result2.delta_ms == -10.0
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 110.000")]


def test_corrected_action_appends_runtime_correction_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "runtime_corrections.jsonl"
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls, runtime_corrections_path=path)
    _establish_baseline(actuator)

    result = actuator.apply(MAC, 377.0, 370.0, 20.0)

    assert result.action == "corrected"
    event = json.loads(path.read_text(encoding="utf-8").strip())
    assert event["action"] == "corrected"
    assert event["event"] == "runtime_correction"
    assert event["mac"] == MAC
    assert event["measured_latency_ms"] == 377.0
    assert event["target_total_ms"] == 370.0
    assert event["current_filter_delay_ms"] == 20.0
    assert event["delta_ms"] == 7.0
    assert event["new_filter_delay_ms"] == 13.0
