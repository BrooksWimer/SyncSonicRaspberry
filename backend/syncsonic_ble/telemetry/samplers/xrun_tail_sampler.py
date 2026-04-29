"""journalctl tail sampler for PipeWire / WirePlumber graph xruns.

Runs ``journalctl -u syncsonic.service -f --since now`` in a long-lived
subprocess and parses each line as it arrives. Emits one ``pw_xrun``
event per graph-xrun event seen, with the node name, internal id,
pending queue depth, waiting and process times in microseconds, and
status (triggered / awake / etc.).

Why this shape and not a poll
-----------------------------
xruns are point events with sub-millisecond resolution. A 1 Hz poll of
the journal would lose ordering information and would have to dedupe
against a moving cursor. journalctl --follow gives us live append-only
delivery and a single subprocess for the lifetime of the collector.

Two log line formats to handle
-----------------------------
PipeWire:
    pw.node: (virtual_out-74) graph xrun not-triggered (0 suppressed)
    pw.node: (virtual_out-74) xrun state:0xf6902008 pending:1/10 s:N a:N f:N waiting:497111 process:11425 status:triggered

WirePlumber (drops the node name on the detail line):
    (bluez_output.F4_6A_DD_D4_F3_C8.1-92) graph xrun not-triggered (0 suppressed)
    (-92) xrun state:0xf7361008 pending:1/1 s:N a:N f:N waiting:399352 process:75981 status:triggered

The header line tells us which node the xrun is on (always present and
always carries the human-readable node name); the detail line carries
the actual queue depth and timing. We remember the (id -> node_name)
mapping from the header lines and look it up when the matching detail
line arrives.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from typing import Dict, Optional

from syncsonic_ble.telemetry import EventType
from syncsonic_ble.telemetry.event_writer import emit
from syncsonic_ble.telemetry.samplers.base import Sampler
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

# Header line: "(<NODE_NAME>-<ID>) graph xrun ..."
_XRUN_HEADER_RE = re.compile(
    r"\(([A-Za-z0-9_.-]+)-(\d+)\)\s+graph\s+xrun\b"
)

# Detail line: "(<NODE_NAME or empty>-<ID>) xrun state:... pending:A/B s:N a:N f:N waiting:US process:US status:WORD"
_XRUN_DETAIL_RE = re.compile(
    r"\(([A-Za-z0-9_.-]*)-(\d+)\)\s+xrun\s+"
    r"state:\S+\s+pending:(\d+)/(\d+)\s+"
    r"s:\d+\s+a:\d+\s+f:\d+\s+"
    r"waiting:(\d+)\s+process:(\d+)\s+"
    r"status:(\w+)"
)

# Don't let the id->name memory grow unbounded if PipeWire keeps creating
# nodes; small bound is fine because xrun headers and details arrive
# back-to-back in the same millisecond.
_NODE_NAME_CACHE_MAX = 256


class XrunTailSampler(Sampler):
    """Long-running journalctl tail. Owns a daemon thread.

    Unlike the polling samplers, this one's tick() is a no-op. The
    actual work runs on the background thread started in setup().
    """

    name = "xrun_tail"
    interval_sec = 60.0  # tick() is a no-op; this is only the heartbeat cadence

    def __init__(self, unit: str = "syncsonic.service") -> None:
        super().__init__()
        self._unit = unit
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._id_to_node: Dict[int, str] = {}

    def setup(self) -> None:
        try:
            self._proc = subprocess.Popen(
                [
                    "journalctl",
                    "-u",
                    self._unit,
                    "-f",
                    "--since",
                    "now",
                    "-o",
                    "short",
                    "--no-pager",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            log.warning("XrunTailSampler: journalctl not found, disabling: %s", exc)
            return
        self._thread = threading.Thread(
            target=self._reader_loop,
            name="syncsonic-xrun-tail",
            daemon=True,
        )
        self._thread.start()

    def teardown(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._proc.kill()
                except OSError:
                    pass
        self._proc = None

    def tick(self) -> None:
        # No-op: the reader thread does all the work. Heartbeat-only.
        return

    def _reader_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        for raw_line in self._proc.stdout:
            if self._stop.is_set():
                return
            try:
                self._handle_line(raw_line)
            except Exception as exc:  # noqa: BLE001
                log.debug("XrunTailSampler line handler failed: %s", exc)

    def _handle_line(self, line: str) -> None:
        # Header lines update our id -> node_name memory.
        m = _XRUN_HEADER_RE.search(line)
        if m:
            node_name = m.group(1)
            try:
                node_id = int(m.group(2))
            except ValueError:
                return
            self._id_to_node[node_id] = node_name
            # Periodically prune; cheap dict-recreate.
            if len(self._id_to_node) > _NODE_NAME_CACHE_MAX:
                self._id_to_node = dict(list(self._id_to_node.items())[-_NODE_NAME_CACHE_MAX // 2 :])
            return

        # Detail lines are where we emit the event with full numbers.
        m = _XRUN_DETAIL_RE.search(line)
        if not m:
            return
        node_name_inline = m.group(1)
        try:
            node_id = int(m.group(2))
            pending_now = int(m.group(3))
            pending_max = int(m.group(4))
            waiting_us = int(m.group(5))
            process_us = int(m.group(6))
        except ValueError:
            return
        status = m.group(7)
        node_name = node_name_inline or self._id_to_node.get(node_id, "")
        emit(EventType.PW_XRUN, {
            "node_id": node_id,
            "node_name": node_name,
            "pending_now": pending_now,
            "pending_max": pending_max,
            "waiting_us": waiting_us,
            "process_us": process_us,
            "status": status,
        })
