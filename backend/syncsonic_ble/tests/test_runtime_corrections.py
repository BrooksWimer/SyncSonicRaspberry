from __future__ import annotations

import json
import os
import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("RESERVED_HCI", "hci0")

from syncsonic_ble.runtime_corrections import RuntimeCorrectionWatcher  # noqa: E402
from syncsonic_ble.utils.constants import Msg  # noqa: E402


def test_runtime_correction_watcher_forwards_recognised_runtime_correction_phase(tmp_path: Path) -> None:
    path = tmp_path / "runtime_corrections.jsonl"
    sent: list[tuple[Msg, dict]] = []
    watcher = RuntimeCorrectionWatcher(lambda msg, payload: sent.append((msg, payload)), path=path)

    path.write_text("", encoding="utf-8")
    watcher._drain_available()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "phase": "runtime_correction",
                    "action": "building_window",
                    "mac": "AA:BB:CC:DD:EE:FF",
                },
            )
            + "\n",
        )

    watcher._drain_available()

    assert sent == [
        (
            Msg.CALIBRATION_RESULT,
            {
                "phase": "runtime_correction",
                "action": "building_window",
                "mac": "AA:BB:CC:DD:EE:FF",
            },
        ),
    ]


def test_runtime_correction_watcher_skips_unrecognised_phase(tmp_path: Path) -> None:
    path = tmp_path / "runtime_corrections.jsonl"
    sent: list[tuple[Msg, dict]] = []
    watcher = RuntimeCorrectionWatcher(lambda msg, payload: sent.append((msg, payload)), path=path)

    path.write_text("", encoding="utf-8")
    watcher._drain_available()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "phase": "ignored_phase",
                    "action": "corrected",
                    "mac": "AA:BB:CC:DD:EE:FF",
                },
            )
            + "\n",
        )

    watcher._drain_available()

    assert sent == []


def test_runtime_correction_watcher_injects_phase_for_legacy_corrected_line(tmp_path: Path) -> None:
    path = tmp_path / "runtime_corrections.jsonl"
    sent: list[tuple[Msg, dict]] = []
    watcher = RuntimeCorrectionWatcher(lambda msg, payload: sent.append((msg, payload)), path=path)

    path.write_text("", encoding="utf-8")
    watcher._drain_available()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "phase": None,
                    "action": "corrected",
                    "timestamp_iso": "2026-05-31T21:57:28Z",
                    "mac": "AA:BB:CC:DD:EE:FF",
                    "measured_latency_ms": 377.0,
                    "target_total_ms": 370.0,
                    "current_filter_delay_ms": 20.0,
                    "delta_ms": 7.0,
                    "new_filter_delay_ms": 13.0,
                },
            )
            + "\n",
        )

    watcher._drain_available()

    assert sent == [
        (
            Msg.CALIBRATION_RESULT,
            {
                "phase": "runtime_correction",
                "mac": "AA:BB:CC:DD:EE:FF",
                "action": "corrected",
                "timestamp_iso": "2026-05-31T21:57:28Z",
                "measured_latency_ms": 377.0,
                "target_total_ms": 370.0,
                "current_filter_delay_ms": 20.0,
                "delta_ms": 7.0,
                "new_filter_delay_ms": 13.0,
            },
        ),
    ]
