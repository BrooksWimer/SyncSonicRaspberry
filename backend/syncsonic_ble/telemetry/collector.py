"""Telemetry Collector: owns the Sampler list and runs them on a thread.

Started once at service start (from ``syncsonic_ble.main``), stopped on
service shutdown. All samplers share a single daemon thread - a
100 ms scheduling loop checks each sampler's ``is_due()`` and calls
``tick()`` on the ones whose interval has elapsed. This keeps the
moving-parts count low (no thread-per-sampler) while preserving the
per-sampler interval declarations.

Failure isolation
-----------------
Every ``tick()`` is wrapped in a try/except so one buggy sampler can
never bring the collector down. Persistent failures are counted; the
first 3 exceptions per sampler are logged at WARNING, the rest at
DEBUG, so a misbehaving sampler does not flood the journal.

Setup ordering
--------------
``start()`` calls ``setup()`` on every sampler before the loop begins,
in declaration order. ``stop()`` calls ``teardown()`` in reverse order.
Setup or teardown failures are logged but never raised, again so a
broken sampler cannot prevent the rest from running or the service
from shutting down cleanly.
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

from syncsonic_ble.telemetry import EventType
from syncsonic_ble.telemetry.event_writer import emit, get_event_writer
from syncsonic_ble.telemetry.samplers import Sampler
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

LOOP_TICK_SEC = 0.1
MAX_LOGGED_EXCEPTIONS_PER_SAMPLER = 3


class Collector:
    """Schedule + run a fixed list of Samplers on a single daemon thread."""

    def __init__(self, samplers: List[Sampler]) -> None:
        self._samplers = list(samplers)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._exception_counts: dict[str, int] = {s.name: 0 for s in self._samplers}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # Force the EventWriter to open the file *before* the lifecycle
        # event is emitted, so collector_start.data.telemetry_path
        # actually carries the path the rest of the events will be in.
        writer = get_event_writer()
        writer.ensure_open()
        emit(EventType.COLLECTOR_START, {
            "samplers": [{"name": s.name, "interval_sec": s.interval_sec} for s in self._samplers],
            "telemetry_path": str(writer.path) if writer.path else None,
        })
        for s in self._samplers:
            try:
                s.setup()
            except Exception as exc:  # noqa: BLE001
                log.warning("Collector: sampler %s setup() failed: %s", s.name, exc)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="syncsonic-telemetry-collector",
            daemon=True,
        )
        self._thread.start()
        log.info("Telemetry collector started with %d samplers", len(self._samplers))

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._thread = None
        for s in reversed(self._samplers):
            try:
                s.teardown()
            except Exception as exc:  # noqa: BLE001
                log.warning("Collector: sampler %s teardown() failed: %s", s.name, exc)
        emit(EventType.COLLECTOR_STOP, {
            "n_emitted": get_event_writer().n_emitted,
            "n_dropped": get_event_writer().n_dropped,
        })
        get_event_writer().close()
        log.info("Telemetry collector stopped")

    def _run(self) -> None:
        # Stagger initial due times so all samplers don't tick at exactly
        # the same monotonic instant on the first iteration. Real cost is
        # avoiding a CPU spike at startup.
        now = time.monotonic()
        for i, s in enumerate(self._samplers):
            s._next_due_monotonic = now + (i * 0.05)  # noqa: SLF001 - we own this state

        while not self._stop.is_set():
            now = time.monotonic()
            for s in self._samplers:
                if not s.is_due(now):
                    continue
                try:
                    s.tick()
                except Exception as exc:  # noqa: BLE001
                    self._note_exception(s, exc)
                s.mark_ticked(now)
            self._stop.wait(LOOP_TICK_SEC)

    def _note_exception(self, sampler: Sampler, exc: BaseException) -> None:
        count = self._exception_counts.get(sampler.name, 0) + 1
        self._exception_counts[sampler.name] = count
        if count <= MAX_LOGGED_EXCEPTIONS_PER_SAMPLER:
            log.warning(
                "Collector: sampler %s tick() raised %s (count=%d)",
                sampler.name,
                type(exc).__name__,
                count,
            )
        else:
            log.debug(
                "Collector: sampler %s tick() raised %s (count=%d, suppressed)",
                sampler.name,
                type(exc).__name__,
                count,
            )


_COLLECTOR: Optional[Collector] = None


def build_default_collector(bus=None) -> Collector:
    """Construct the standard Slice 1 collector with all four samplers.

    The bus arg is the existing dbus.SystemBus from syncsonic_ble.main;
    sharing it avoids opening a second connection per sampler.
    """
    # Imports inside the function to keep top-of-file imports tidy and
    # to make import failures of any one sampler not break the package.
    from syncsonic_ble.telemetry.samplers.bluez_transport_sampler import BluezTransportSampler
    from syncsonic_ble.telemetry.samplers.pw_node_sampler import PwNodeSampler
    from syncsonic_ble.telemetry.samplers.rssi_sampler import RssiSampler
    from syncsonic_ble.telemetry.samplers.xrun_tail_sampler import XrunTailSampler

    return Collector([
        RssiSampler(bus=bus),
        PwNodeSampler(),
        BluezTransportSampler(bus=bus),
        XrunTailSampler(),
    ])


def get_collector() -> Optional[Collector]:
    return _COLLECTOR


def set_collector(c: Collector) -> None:
    global _COLLECTOR
    _COLLECTOR = c
