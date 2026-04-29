"""Thread-safe jsonl event writer for the Slice 1 telemetry stream.

Owns one open file per service start (named with the UTC-iso8601 of the
process start). Every emit() takes a Lock briefly, fills in the
top-level schema fields (``schema_version``, ``monotonic_ns``,
``wall_iso``), serialises to a single JSON line with sorted keys and
ASCII escapes, writes, and (every FSYNC_EVERY_N events) fsyncs so a Pi
power-cut loses at most ~1 second of telemetry.

Time fields - the contract
--------------------------
``monotonic_ns`` is ``time.monotonic_ns()`` at emit time. It is the
canonical join key for events emitted from the same process. Differences
between two events from the same process are meaningful (the units are
real nanoseconds). Comparisons across two service starts, or against the
mic_capture process which has its own monotonic origin, are NOT
meaningful.

``wall_iso`` is the UTC ISO-8601 wall-clock timestamp at emit time, with
millisecond precision and a trailing ``Z``. It is the join key across
processes (collector vs mic_capture) and across service restarts. The
session runner uses wall_iso to slice events into a session window and
to align the mic WAV.

Performance and crash-safety
----------------------------
At expected steady-state load (~5 events/sec with 3 BT speakers) the
writer is essentially free. The Lock is held only for the
``json.dumps`` + ``write`` + occasional ``fsync``, which together are
microseconds. We do NOT spawn a background writer thread or use a
queue; the simplicity is worth the trivial blocking on the caller.

If the directory cannot be created or the file cannot be opened, emit()
silently drops the event and prints to stderr. We intentionally do not
raise; telemetry is observability, not critical-path. A broken
collector must never break the audio service.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

from syncsonic_ble.telemetry import SCHEMA_VERSION, telemetry_root

FSYNC_EVERY_N = 50


def _utc_iso_now_ms() -> str:
    """Return current UTC time as ``2026-04-29T04:50:01.123Z``."""
    now = datetime.now(timezone.utc)
    base = now.strftime("%Y-%m-%dT%H:%M:%S")
    millis = now.microsecond // 1000
    return f"{base}.{millis:03d}Z"


class EventWriter:
    """Thread-safe singleton-style jsonl writer.

    Acquire the global instance via :func:`get_event_writer`. The writer
    opens its file lazily on first emit so that import of this module is
    side-effect-free (important for unit tests and ``compileall``).
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root or telemetry_root()
        self._lock = threading.Lock()
        self._file: Optional[TextIO] = None
        self._path: Optional[Path] = None
        self._n_emitted = 0
        self._n_dropped = 0
        self._open_failed_logged = False

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def ensure_open(self) -> bool:
        """Force-open the events file now (non-lazy). Returns True on success.

        Useful when a caller needs ``self.path`` to be populated *before*
        the first emit (e.g. so the collector_start lifecycle event can
        report it). Safe and idempotent; reuses the existing lock.
        """
        with self._lock:
            return self._ensure_open_locked()

    @property
    def n_emitted(self) -> int:
        return self._n_emitted

    @property
    def n_dropped(self) -> int:
        return self._n_dropped

    def emit(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Append one event to the jsonl. Never raises on writer failure."""
        record = {
            "schema_version": SCHEMA_VERSION,
            "monotonic_ns": time.monotonic_ns(),
            "wall_iso": _utc_iso_now_ms(),
            "event_type": str(event_type),
            "data": data or {},
        }
        try:
            line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        except (TypeError, ValueError) as exc:
            # The data payload was non-serialisable. Drop and stay alive.
            self._note_drop(f"json encode: {exc!r}")
            return
        with self._lock:
            if not self._ensure_open_locked():
                self._note_drop("file not open")
                return
            try:
                assert self._file is not None
                self._file.write(line + "\n")
                self._n_emitted += 1
                if self._n_emitted % FSYNC_EVERY_N == 0:
                    self._file.flush()
                    os.fsync(self._file.fileno())
            except OSError as exc:
                self._note_drop(f"write: {exc!r}")

    def close(self) -> None:
        """Flush + close. Safe to call multiple times. Idempotent."""
        with self._lock:
            if self._file is None:
                return
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
            except OSError:
                pass
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def _ensure_open_locked(self) -> bool:
        if self._file is not None:
            return True
        try:
            events_dir = self._root / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            stamp = _utc_iso_now_ms().replace(":", "-")  # NTFS / shell friendly
            self._path = events_dir / f"syncsonic-events-{stamp}.jsonl"
            # Line-buffered text mode; we still fsync periodically for crash safety.
            self._file = open(self._path, "a", encoding="ascii", buffering=1)
            return True
        except OSError as exc:
            if not self._open_failed_logged:
                print(
                    f"[telemetry.EventWriter] failed to open events jsonl: {exc!r}",
                    file=sys.stderr,
                )
                self._open_failed_logged = True
            return False

    def _note_drop(self, reason: str) -> None:
        self._n_dropped += 1
        # Only print the first few drops so a sustained failure doesn't
        # spam the journal; counters keep counting.
        if self._n_dropped <= 5:
            print(
                f"[telemetry.EventWriter] dropped event ({reason}); total drops={self._n_dropped}",
                file=sys.stderr,
            )


_WRITER: Optional[EventWriter] = None
_WRITER_LOCK = threading.Lock()


def get_event_writer() -> EventWriter:
    """Process-wide singleton accessor. Thread-safe."""
    global _WRITER
    if _WRITER is None:
        with _WRITER_LOCK:
            if _WRITER is None:
                _WRITER = EventWriter()
    return _WRITER


def emit(event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
    """Convenience: emit on the process-wide writer.

    Callsites should prefer this over reaching for the singleton
    explicitly so the import surface stays narrow.
    """
    get_event_writer().emit(event_type, data)
