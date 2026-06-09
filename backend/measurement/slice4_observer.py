"""Slice 4 observe-only proposal recording."""

from __future__ import annotations

import csv
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_OBSERVATION_PATH = Path("/var/lib/syncsonic/slice4-observations.csv")

CSV_COLUMNS = [
    "timestamp_iso",
    "speaker_id",
    "measured_latency_ms",
    "proposed_adjustment_ppm",
    "actuation_applied_ppm",
    "confidence",
    "current_filter_delay_ms",
    "missed_burst",
    "snr_db",
    "history_json",
    "pattern_state_json",
]


class ObservationWriter:
    """Append Slice 4 observations to CSV and journal JSON-lines."""

    def __init__(
        self,
        path: Path | str = DEFAULT_OBSERVATION_PATH,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.path = Path(path)
        self.logger = logger or logging.getLogger("measurement.slice4_observer")
        self._file: Optional[Any] = None
        self._writer: Optional[csv.DictWriter[Any]] = None

    def __enter__(self) -> "ObservationWriter":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def start(self) -> None:
        if self._file is not None:
            return
        try:
            write_header = not self.path.exists()
            if not write_header:
                self._migrate_header_if_needed()
            self._file = self.path.open("a", encoding="utf-8", newline="")
            self._writer = csv.DictWriter(self._file, fieldnames=CSV_COLUMNS)
            if write_header:
                self._writer.writeheader()
                self._file.flush()
        except OSError as exc:
            message = f"Slice 4 observation path is not writable: {self.path}"
            self.logger.error("%s (%r)", message, exc)
            raise RuntimeError(message) from exc

    def _migrate_header_if_needed(self) -> None:
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames == CSV_COLUMNS:
                return
            if not reader.fieldnames:
                return
            rows = list(reader)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                migrated = {column: row.get(column, "") for column in CSV_COLUMNS}
                migrated["actuation_applied_ppm"] = row.get("actuation_applied_ppm") or 0.0
                writer.writerow(migrated)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file = None
        self._writer = None

    def write_observation(
        self,
        *,
        speaker_id: str,
        measured_latency_ms: float,
        history_snapshot: list[Any],
        pattern_state_snapshot: dict[str, Any],
        proposed_adjustment_ppm: float,
        confidence: float,
        current_filter_delay_ms: float,
        missed_burst: bool,
        snr_db: float,
        actuation_applied_ppm: float = 0.0,
        timestamp_iso: Optional[str] = None,
    ) -> dict[str, Any]:
        if self._writer is None:
            raise RuntimeError("Slice 4 observation writer has not been started")

        timestamp = timestamp_iso or datetime.now(timezone.utc).isoformat()
        journal_record = {
            "event": "slice4_observation",
            "timestamp_iso": timestamp,
            "speaker_id": speaker_id,
            "measured_latency_ms": _json_number(measured_latency_ms),
            "history_snapshot": _json_clean(history_snapshot),
            "pattern_state_snapshot": _json_clean(pattern_state_snapshot),
            "proposed_adjustment_ppm": _json_number(proposed_adjustment_ppm),
            "actuation_applied_ppm": _json_number(actuation_applied_ppm),
            "confidence": _json_number(confidence),
            "current_filter_delay_ms": _json_number(current_filter_delay_ms),
            "missed_burst": bool(missed_burst),
            "snr_db": _json_number(snr_db),
        }
        row = {
            "timestamp_iso": timestamp,
            "speaker_id": speaker_id,
            "measured_latency_ms": _csv_number(measured_latency_ms),
            "proposed_adjustment_ppm": _csv_number(proposed_adjustment_ppm),
            "actuation_applied_ppm": _csv_number(actuation_applied_ppm),
            "confidence": _csv_number(confidence),
            "current_filter_delay_ms": _csv_number(current_filter_delay_ms),
            "missed_burst": str(bool(missed_burst)),
            "snr_db": _csv_number(snr_db),
            "history_json": json.dumps(_json_clean(history_snapshot), sort_keys=True, separators=(",", ":")),
            "pattern_state_json": json.dumps(
                _json_clean(pattern_state_snapshot),
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        self._writer.writerow(row)
        assert self._file is not None
        self._file.flush()
        self.logger.info(json.dumps(journal_record, sort_keys=True, separators=(",", ":")))
        return journal_record

    def write_missed_burst(
        self,
        *,
        speaker_id: str,
        current_filter_delay_ms: float,
        history_snapshot: list[Any],
        pattern_state_snapshot: dict[str, Any],
        timestamp_iso: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.write_observation(
            timestamp_iso=timestamp_iso,
            speaker_id=speaker_id,
            measured_latency_ms=math.nan,
            history_snapshot=history_snapshot,
            pattern_state_snapshot=pattern_state_snapshot,
            proposed_adjustment_ppm=math.nan,
            actuation_applied_ppm=0.0,
            confidence=math.nan,
            current_filter_delay_ms=current_filter_delay_ms,
            missed_burst=True,
            snr_db=math.nan,
        )


def _csv_number(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN"
    return value


def _json_number(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json_clean(value: Any) -> Any:
    if isinstance(value, float):
        return _json_number(value)
    if isinstance(value, dict):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_clean(item) for item in value]
    return value
