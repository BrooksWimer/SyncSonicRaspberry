from __future__ import annotations

import logging
import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from measurement.slice5_actuator import ACTIVE, SUSPENDED, SpeakerActuator  # noqa: E402


def _writer(calls: list[tuple[str, str]]):
    def write(path: Path, command: str):
        calls.append((str(path), command))
        return {"ok": True}

    return write


def _proposal(mac: str = "AA:BB:CC:DD:EE:FF", **overrides):
    record = {
        "event": "relative_correction_proposed",
        "mac": mac,
        "proposed_adjustment_ppm": 12.5,
        "residual_ms": 1.0,
        "current_filter_delay_ms": 100.0,
        "missed_burst": False,
        "max_ppm": 50.0,
    }
    record.update(overrides)
    return record


def _active_actuator(calls: list[tuple[str, str]], *, mac: str = "AA:BB:CC:DD:EE:FF") -> SpeakerActuator:
    actuator = SpeakerActuator(
        {mac: Path(f"/tmp/{mac.replace(':', '_')}.sock")},
        warmup_cycles_required=1,
        socket_writer=_writer(calls),
    )
    actuator.apply(_proposal(mac))
    assert actuator.state_for(mac) == ACTIVE
    calls.clear()
    return actuator


def test_warming_up_blocks_actuation() -> None:
    calls: list[tuple[str, str]] = []
    actuator = SpeakerActuator(
        {"AA:BB:CC:DD:EE:FF": Path("/tmp/a.sock")},
        socket_writer=_writer(calls),
    )

    result = actuator.apply(_proposal())

    assert result.actuation_applied_ppm == 0.0
    assert result.skip_reason == "WARMING_UP"
    assert calls == []


def test_warmup_to_active_after_n_clean_cycles() -> None:
    calls: list[tuple[str, str]] = []
    actuator = SpeakerActuator(
        {"AA:BB:CC:DD:EE:FF": Path("/tmp/a.sock")},
        warmup_cycles_required=3,
        socket_writer=_writer(calls),
    )

    for _idx in range(3):
        result = actuator.apply(_proposal())

    assert result.state == ACTIVE
    assert result.actuation_applied_ppm == 0.0
    assert actuator.state_for("AA:BB:CC:DD:EE:FF") == ACTIVE
    assert calls == []


def test_clamped_proposal_skipped(caplog) -> None:
    calls: list[tuple[str, str]] = []
    actuator = _active_actuator(calls)

    with caplog.at_level(logging.INFO):
        result = actuator.apply(_proposal(proposed_adjustment_ppm=50.0, max_ppm=50.0))

    assert result.actuation_applied_ppm == 0.0
    assert result.skip_reason == "SKIP_CLAMPED"
    assert calls == []
    assert "SKIP_CLAMPED" in caplog.text


def test_slider_applied_for_large_residual() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _active_actuator(calls)

    result = actuator.apply(_proposal(residual_ms=6.0, current_filter_delay_ms=100.0))

    assert result.actuation_applied_ppm == 0.0
    assert result.slider_applied_ms == 6.0
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_delay 106.000")]


def test_ppm_applied_for_small_residual() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _active_actuator(calls)

    result = actuator.apply(_proposal(proposed_adjustment_ppm=75.0, residual_ms=5.0, max_ppm=100.0))

    assert result.actuation_applied_ppm == 50.0
    assert result.slider_applied_ms == 0.0
    assert calls == [("/tmp/AA_BB_CC_DD_EE_FF.sock", "set_rate_ppm 50.000")]


def test_auto_suspend_on_high_miss_rate() -> None:
    calls: list[tuple[str, str]] = []
    actuator = _active_actuator(calls)

    for idx in range(10):
        actuator.apply(_proposal(missed_burst=idx < 4))

    assert actuator.state_for("AA:BB:CC:DD:EE:FF") == SUSPENDED


def test_emergency_stop_zeros_all_ppm() -> None:
    calls: list[tuple[str, str]] = []
    actuator = SpeakerActuator(
        {
            "AA:BB:CC:DD:EE:FF": Path("/tmp/a.sock"),
            "11:22:33:44:55:66": Path("/tmp/b.sock"),
        },
        socket_writer=_writer(calls),
    )

    actuator.emergency_stop()

    assert calls == [
        ("/tmp/b.sock", "set_rate_ppm 0"),
        ("/tmp/a.sock", "set_rate_ppm 0"),
    ]
    assert actuator.state_for("AA:BB:CC:DD:EE:FF") == SUSPENDED
    assert actuator.state_for("11:22:33:44:55:66") == SUSPENDED


def test_independent_per_speaker_state() -> None:
    calls: list[tuple[str, str]] = []
    suspended_mac = "AA:BB:CC:DD:EE:FF"
    active_mac = "11:22:33:44:55:66"
    actuator = SpeakerActuator(
        {
            suspended_mac: Path("/tmp/a.sock"),
            active_mac: Path("/tmp/b.sock"),
        },
        warmup_cycles_required=1,
        socket_writer=_writer(calls),
    )
    actuator.apply(_proposal(suspended_mac))
    actuator.apply(_proposal(active_mac))
    calls.clear()

    for idx in range(10):
        actuator.apply(_proposal(suspended_mac, missed_burst=idx < 4))
    active_result = actuator.apply(_proposal(active_mac, proposed_adjustment_ppm=-10.0))

    assert actuator.state_for(suspended_mac) == SUSPENDED
    assert actuator.state_for(active_mac) == ACTIVE
    assert active_result.actuation_applied_ppm == -10.0
    assert calls[-1] == ("/tmp/b.sock", "set_rate_ppm -10.000")
