from __future__ import annotations

import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from measurement.slice5_actuator import ACTIVE, SpeakerActuator  # noqa: E402


MAC = "AA:BB:CC:DD:EE:FF"


def _writer(calls: list[tuple[str, str]]):
    def write(path: Path, command: str):
        calls.append((str(path), command))
        return {"ok": True}

    return write


def _proposal(**overrides):
    record = {
        "event": "relative_correction_proposed",
        "mac": MAC,
        "proposed_adjustment_ppm": 12.5,
        "residual_ms": 1.0,
        "measured_latency_ms": 100.0,
        "current_filter_delay_ms": 200.0,
        "missed_burst": False,
        "max_ppm": 50.0,
    }
    record.update(overrides)
    return record


def _actuator(calls: list[tuple[str, str]], **kwargs) -> SpeakerActuator:
    return SpeakerActuator(
        {MAC: Path("/tmp/slice6.sock")},
        socket_writer=_writer(calls),
        **kwargs,
    )


def _active_actuator(calls: list[tuple[str, str]], **kwargs) -> SpeakerActuator:
    actuator = _actuator(calls, warmup_cycles_required=1, **kwargs)
    for _idx in range(5):
        actuator.apply(_proposal(measured_latency_ms=100.0))
    assert actuator.state_for(MAC) == ACTIVE
    calls.clear()
    return actuator


def _slider_commands(calls: list[tuple[str, str]]) -> list[str]:
    return [command for _path, command in calls if command.startswith("set_delay ")]


def test_baseline_established_after_n_samples() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _actuator(calls, warmup_cycles_required=99, baseline_warmup_n=5)

    for latency_ms in [100.0, 105.0, 90.0, 110.0, 95.0]:
        actuator.apply(_proposal(measured_latency_ms=latency_ms))

    assert actuator.baseline_for(MAC) == 100.0


def test_no_slider_below_threshold() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _active_actuator(calls)

    actuator.apply(_proposal(measured_latency_ms=104.0, current_filter_delay_ms=200.0))

    assert _slider_commands(calls) == []


def test_slider_fires_above_threshold() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _active_actuator(calls)

    result = actuator.apply(_proposal(measured_latency_ms=150.0, current_filter_delay_ms=200.0))

    assert result.slider_applied_ms == -50.0
    assert _slider_commands(calls) == ["set_delay 150.000"]


def test_slider_cooldown_blocks_repeat_fire() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _active_actuator(calls, slider_cooldown_cycles=3)

    actuator.apply(_proposal(measured_latency_ms=150.0, current_filter_delay_ms=200.0))
    actuator.apply(_proposal(measured_latency_ms=150.0, current_filter_delay_ms=200.0))
    actuator.apply(_proposal(measured_latency_ms=150.0, current_filter_delay_ms=200.0))
    actuator.apply(_proposal(measured_latency_ms=150.0, current_filter_delay_ms=200.0))

    assert _slider_commands(calls) == ["set_delay 150.000", "set_delay 150.000"]


def test_ppm_path_unaffected() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _active_actuator(calls)

    result = actuator.apply(
        _proposal(
            measured_latency_ms=100.0,
            proposed_adjustment_ppm=-17.25,
            current_filter_delay_ms=200.0,
        )
    )

    assert result.actuation_applied_ppm == -17.25
    assert _slider_commands(calls) == []
    assert calls == [("/tmp/slice6.sock", "set_rate_ppm -17.250")]


def test_sigusr1_blocks_slider(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    actuator = _active_actuator(calls)
    monkeypatch.setenv("MAVERICK_CORRECTION_STOP", "1")

    result = actuator.apply(_proposal(measured_latency_ms=400.0, current_filter_delay_ms=200.0))

    assert result.skip_reason == "EMERGENCY_STOP"
    assert _slider_commands(calls) == []
    assert calls == [("/tmp/slice6.sock", "set_rate_ppm 0")]
