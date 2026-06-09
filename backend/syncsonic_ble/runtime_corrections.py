"""Tail runtime correction JSONL events and bridge them to BLE calibration notifications."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from syncsonic_ble.utils.constants import Msg
from syncsonic_ble.utils.logging_conf import get_logger

RUNTIME_CORRECTIONS_PATH = Path("/run/syncsonic/runtime_corrections.jsonl")

NotificationSink = Callable[[Msg, dict[str, Any]], None]

log = get_logger(__name__)


class RuntimeCorrectionWatcher:
    """Follow the actuator JSONL stream and emit one BLE event per correction."""

    def __init__(
        self,
        notification_sink: NotificationSink,
        *,
        path: Path | str = RUNTIME_CORRECTIONS_PATH,
        poll_interval_sec: float = 0.25,
    ) -> None:
        self.notification_sink = notification_sink
        self.path = Path(path)
        self.poll_interval_sec = poll_interval_sec
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offset = 0
        self._inode: Optional[int] = None
        self._initialize_tail_position()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="syncsonic-runtime-correction-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _initialize_tail_position(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return
        self._inode = stat.st_ino
        self._offset = stat.st_size

    def _run(self) -> None:
        log.info("Runtime correction watcher tailing %s", self.path)
        while not self._stop.is_set():
            try:
                self._drain_available()
            except Exception as exc:  # noqa: BLE001 - watcher must not kill BLE runtime.
                log.warning("Runtime correction watcher error: %s", exc)
            self._stop.wait(self.poll_interval_sec)

    def _drain_available(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._offset = 0
            self._inode = None
            return

        inode = stat.st_ino
        if self._inode != inode:
            self._inode = inode
            self._offset = 0
        if stat.st_size < self._offset:
            self._offset = 0

        with self.path.open("r", encoding="utf-8") as fh:
            fh.seek(self._offset)
            for line in fh:
                self._handle_line(line)
            self._offset = fh.tell()

    def _handle_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:
            log.warning("Skipping malformed runtime correction JSONL line: %s", exc)
            return
        # Forward all events that carry recognised phases so the frontend
        # can render the full progression (building_window → within_threshold → corrected)
        # as well as silent_align lifecycle events.
        if event.get("phase") is None and event.get("action") == "corrected":
            event["phase"] = "runtime_correction"
        if event.get("phase") not in self._FORWARDED_PHASES:
            return
        self.notification_sink(Msg.CALIBRATION_RESULT, event)


def build_and_start_runtime_correction_watcher(
    notification_sink: NotificationSink,
    *,
    path: Path | str = RUNTIME_CORRECTIONS_PATH,
) -> RuntimeCorrectionWatcher:
    watcher = RuntimeCorrectionWatcher(notification_sink, path=path)
    watcher.start()
    return watcher
