from __future__ import annotations

import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from measurement.slice5_actuator import SpeakerActuator  # noqa: E402


MAC = "AA:BB:CC:DD:EE:FF"


def _writer(calls: list[tuple[str, str]]):
    def write(path: Path, command: str):
        calls.append((str(path), command))
        return {"ok": True}

    return write


def _actuator(calls: list[tuple[str, str]], *, mac: str = MAC) -> SpeakerActuator:
    return SpeakerActuator(
        {mac: Path(f"/tmp/{mac.replace(':', '_')}.sock")},
        socket_writer=_writer(calls),
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
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 27.000")]


def test_freak_outlier_skips_without_corrupting_baseline() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls)
    _establish_baseline(actuator)

    result = actuator.apply(MAC, 500.0, 370.0, 20.0)

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
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 15.000")]
