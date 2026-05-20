from __future__ import annotations

import subprocess
import wave
from pathlib import Path

from measurement.probe_signals import RUNTIME_BURST_DURATION_SEC, build_runtime_ultrasonic_burst
from syncsonic_ble.helpers import arrival_burst_actuation as actuation


class FakeActuationManager:
    def get_status_snapshot(self):
        return {
            "AA:BB:CC:DD:EE:01": {
                "active": True,
                "delay_cmd_ms": 245.0,
            },
            "AA:BB:CC:DD:EE:02": {
                "active": True,
                "delay_cmd_ms": 510.0,
            },
            "AA:BB:CC:DD:EE:03": {
                "active": False,
                "delay_cmd_ms": 999.0,
            },
        }


def test_runtime_ultrasonic_burst_is_fixed_100ms() -> None:
    burst = build_runtime_ultrasonic_burst()

    assert abs((burst.size / 48_000.0) - RUNTIME_BURST_DURATION_SEC) < 0.001
    assert RUNTIME_BURST_DURATION_SEC == 0.100


def test_detection_window_is_anchored_to_actuation_delay_not_burst_duration() -> None:
    window = actuation.detection_window_for_delay(
        750.0,
        scheduled_start_sec=2.0,
        margin_ms=125.0,
    )

    assert window.expected_arrival_sec == 2.750
    assert window.start_sec == 2.625
    assert window.end_sec == 2.875


def test_plan_arrival_bursts_targets_each_speaker_sink_and_delay() -> None:
    targets = actuation.plan_arrival_bursts(
        {"AA:BB:CC:DD:EE:02": 510.0, "AA:BB:CC:DD:EE:01": 245.0},
        sink_resolver=lambda mac: f"bluez_output.{mac.replace(':', '_')}.a2dp_sink",
    )

    assert [target.mac for target in targets] == ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"]
    assert targets[0].sink == "bluez_output.AA_BB_CC_DD_EE_01.a2dp_sink"
    assert targets[0].burst_duration_ms == 100.0
    assert targets[0].detection_window.expected_arrival_sec == 0.245
    assert targets[1].detection_window.expected_arrival_sec == 1.510


def test_emit_once_uses_paplay_device_without_mute_or_duck(monkeypatch) -> None:
    commands: list[list[str]] = []

    monkeypatch.setattr(actuation.shutil, "which", lambda name: "/usr/bin/paplay")
    monkeypatch.setattr(actuation.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        actuation,
        "resolve_pipewire_output_name",
        lambda mac: f"bluez_output.{mac.replace(':', '_')}.a2dp_sink",
    )

    def fake_run(command, **kwargs):
        commands.append(list(command))
        wav_path = Path(command[-1])
        with wave.open(str(wav_path), "rb") as wav:
            assert wav.getframerate() == 48_000
            assert wav.getnframes() == 4_800
        return subprocess.CompletedProcess(command, 0, "", "")

    actuator = actuation.ArrivalBurstActuator(
        manager=FakeActuationManager(),
        run_command=fake_run,
        clock=lambda: 0.0,
    )

    results = actuator.emit_once()

    assert all(result.ok for result in results)
    assert commands == [
        [
            "paplay",
            "--device=bluez_output.AA_BB_CC_DD_EE_01.a2dp_sink",
            commands[0][2],
        ],
        [
            "paplay",
            "--device=bluez_output.AA_BB_CC_DD_EE_02.a2dp_sink",
            commands[1][2],
        ],
    ]
    assert not any("mute" in part or "duck" in part for command in commands for part in command)
