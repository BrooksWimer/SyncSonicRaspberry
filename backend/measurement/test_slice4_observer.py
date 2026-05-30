from __future__ import annotations

import csv
import io
import logging
import math
import sys
import types
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

sys.modules.setdefault("dbus", types.SimpleNamespace(SystemBus=lambda: None))

from measurement.runtime_latency_service import RuntimeSyncService, SpeakerTarget, _build_parser  # noqa: E402
from measurement.slice4_observer import CSV_COLUMNS, ObservationWriter  # noqa: E402


def _logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(f"slice4-test-{id(stream)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers[:] = [logging.StreamHandler(stream)]
    return logger, stream


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _service_args(path: Path, *extra: str):
    return _build_parser().parse_args(
        [
            "--detector-mode",
            "pattern",
            "--slice4-observation-path",
            str(path),
            *extra,
        ]
    )


def test_csv_created_with_header(tmp_path: Path) -> None:
    path = tmp_path / "slice4.csv"
    logger, _stream = _logger()

    with ObservationWriter(path, logger=logger):
        pass

    assert path.read_text(encoding="utf-8").splitlines() == [",".join(CSV_COLUMNS)]


def test_csv_appends_without_duplicate_header(tmp_path: Path) -> None:
    path = tmp_path / "slice4.csv"
    logger, _stream = _logger()

    with ObservationWriter(path, logger=logger) as writer:
        writer.write_observation(
            timestamp_iso="2026-05-30T00:00:00+00:00",
            speaker_id="A",
            measured_latency_ms=100.0,
            history_snapshot=[100.0],
            pattern_state_snapshot={"event": "first"},
            proposed_adjustment_ppm=-1.0,
            confidence=1.0,
            current_filter_delay_ms=12.0,
            missed_burst=False,
            snr_db=25.0,
        )
    with ObservationWriter(path, logger=logger) as writer:
        writer.write_missed_burst(
            timestamp_iso="2026-05-30T00:00:01+00:00",
            speaker_id="A",
            current_filter_delay_ms=12.0,
            history_snapshot=[100.0],
            pattern_state_snapshot={"event": "second"},
        )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines.count(",".join(CSV_COLUMNS)) == 1
    assert len(lines) == 3


def test_unwritable_path_raises_on_startup(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "slice4.csv"
    logger, _stream = _logger()

    def raise_permission(*_args, **_kwargs):
        raise PermissionError("blocked")

    monkeypatch.setattr(Path, "open", raise_permission)

    try:
        ObservationWriter(path, logger=logger).start()
    except RuntimeError as exc:
        assert str(path) in str(exc)
    else:
        raise AssertionError("RuntimeError not raised")


def test_full_proposal_row_written(tmp_path: Path) -> None:
    path = tmp_path / "slice4.csv"
    args = _service_args(path, "--slice4-observe")
    service = RuntimeSyncService(args)
    assert service.slice4_observer is not None
    target = SpeakerTarget(mac="AA:BB:CC:DD:EE:FF", socket_path=tmp_path / "sock")
    target.latency_history_ms = [101.0, 102.5]
    proposal = {
        "event": "relative_correction_proposed",
        "reason": "group_residual",
        "proposed_rate_ppm": -7.25,
        "relative_residual_ms": 1.45,
    }

    with service.slice4_observer:
        service._record_slice4_observation(
            target,
            measured_latency_ms=103.0,
            current_filter_delay_ms=13.5,
            snr_db=22.0,
            proposal_record=proposal,
            pattern_state_snapshot={"relative_proposal": proposal},
        )

    row = _rows(path)[0]
    assert row["speaker_id"] == "AA:BB:CC:DD:EE:FF"
    assert float(row["measured_latency_ms"]) == 103.0
    assert float(row["proposed_adjustment_ppm"]) == -7.25
    assert float(row["confidence"]) == 1.0
    assert float(row["current_filter_delay_ms"]) == 13.5
    assert row["missed_burst"] == "False"
    assert float(row["snr_db"]) == 22.0
    assert "relative_correction_proposed" in row["pattern_state_json"]


def test_missed_burst_row_is_nan(tmp_path: Path) -> None:
    path = tmp_path / "slice4.csv"
    args = _service_args(path, "--slice4-observe")
    service = RuntimeSyncService(args)
    assert service.slice4_observer is not None
    target = SpeakerTarget(mac="AA:BB:CC:DD:EE:FF", socket_path=tmp_path / "sock")
    target.latency_history_ms = [101.0]

    with service.slice4_observer:
        service._record_slice4_missed_burst(
            target,
            current_filter_delay_ms=13.5,
            pattern_state_snapshot={"reason": "pattern_not_matched"},
        )

    row = _rows(path)[0]
    assert math.isnan(float(row["measured_latency_ms"]))
    assert math.isnan(float(row["proposed_adjustment_ppm"]))
    assert math.isnan(float(row["confidence"]))
    assert math.isnan(float(row["snr_db"]))
    assert row["missed_burst"] == "True"
    assert float(row["current_filter_delay_ms"]) == 13.5


def test_observation_disabled_by_default(tmp_path: Path, caplog) -> None:
    path = tmp_path / "slice4.csv"
    args = _service_args(path)
    service = RuntimeSyncService(args)
    target = SpeakerTarget(mac="AA:BB:CC:DD:EE:FF", socket_path=tmp_path / "sock")

    with caplog.at_level(logging.INFO):
        service._record_slice4_missed_burst(
            target,
            current_filter_delay_ms=13.5,
            pattern_state_snapshot={"reason": "pattern_not_matched"},
        )

    assert service.slice4_observer is None
    assert not path.exists()
    assert "slice4_observation" not in caplog.text
