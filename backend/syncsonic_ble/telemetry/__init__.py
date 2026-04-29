"""SyncSonic Slice 1 telemetry package.

This package owns the always-on observability surface for the SyncSonic
backend: a single jsonl event stream per service start, a small set of
sampler threads that pull from the live system at well-defined cadences,
and an event-emission API that the rest of the codebase can call to push
significant events into the same stream.

Why this exists
---------------
The architecture proposal in
``docs/maverick/proposals/05-coordinated-engine-architecture.md`` makes
the case that every later slice (the elastic delay engine in Slice 2,
the System Coordinator in Slice 3, the mic-driven runtime alignment in
Slice 4) needs reproducible, comparable measurements to be worth
shipping. Slice 0 already exposed two production-only bugs that no
static check could have caught (multi-path BlueZ entries and the
phone-ingress closure late-binding). The 2026-04-29 RSSI A/B field
experiment (proposal Section 9) further showed that the system is
RF-limited and that RSSI is the leading indicator of audible dropouts.
This package is what turns those one-off observations into a permanent
observable.

Schema (this is the contract; bump SCHEMA_VERSION when you change it)
---------------------------------------------------------------------

Each line in the jsonl is exactly one JSON object with these top-level
fields:

    schema_version : int   - integer, currently 1
    monotonic_ns   : int   - CLOCK_MONOTONIC ns since service start;
                             the canonical join key across all events
                             and across the mic capture WAV timestamps
    wall_iso       : str   - UTC ISO-8601 with millisecond precision;
                             for human readability only, do not join on
    event_type     : str   - one of the strings in EVENT_TYPES
    data           : dict  - event-type-specific payload; documented in
                             the per-sampler module docstrings

Files always sort their keys, use ASCII escapes, separators=(",",":"),
and end every line with "\n". A truncated final line is recoverable.

event_type catalog
------------------
- collector_start / collector_stop     (lifecycle)
- rssi_sample                          (per-second per speaker)
- rssi_baseline                        (per-10s per speaker, rolling
                                        median window)
- pw_node_snapshot                     (per-second, all PW nodes)
- pw_xrun                              (event-driven, parsed from journal)
- bluez_transport_snapshot             (per-5s per active A2DP transport)
- route_create / route_teardown        (event-driven, from actuation
                                        daemon callsites)
- set_latency_request / set_volume_request
                                       (event-driven, from BLE handlers)
- bluez_connect / bluez_disconnect     (event-driven, from
                                        connection_manager)
- mic_segment_written                  (event-driven, heartbeat from
                                        mic_capture process)

Filesystem layout
-----------------
TELEMETRY_ROOT defaults to ``/home/syncsonic/syncsonic-telemetry/`` on
the Pi (persistent, user-writable, no privilege escalation needed).
The path is overridable with the SYNCSONIC_TELEMETRY_ROOT environment
variable so the same code can run during local development.

    <root>/
      events/
        syncsonic-events-<UTC-iso8601>.jsonl   one per service start
      mic/
        rolling-<idx>.wav                       owned by mic_capture
      sessions/
        <NAME>-<UTC-iso8601>/                   one per `make session`
          events.jsonl                          window-extracted copy
          mic.wav                               window-extracted copy
          report.md                             generated single-page
        latest -> <NAME>-<UTC-iso8601>          symlink convenience

Process model
-------------
- The Collector runs as a daemon thread inside syncsonic_ble.main and
  shares the service lifecycle. Owns the samplers and the EventWriter.
- The mic_capture is a separate Python process started by
  start_syncsonic.sh. It writes to TELEMETRY_ROOT/mic/ continuously and
  emits a `mic_segment_written` event per rotation so the collector
  knows the audio side is healthy.
- The session runner (`measurement.run_session`) is a one-shot manual
  invocation. It snapshots the in-flight event jsonl + the rolling mic
  WAV into a session bundle and runs the report generator.

No new dependencies
-------------------
This package uses Python stdlib only (json, threading, subprocess, time,
pathlib, statistics, collections). Existing system packages
(pulseaudio-utils for parecord, bluez for hcitool, pipewire-bin for
pw-dump / pw-cli) cover the data sources. No `pip install` and no `apt
install` is required to run Slice 1 against the existing Pi.
"""

from __future__ import annotations

import os
from pathlib import Path

SCHEMA_VERSION = 1

# All event_type strings live here so a typo at a callsite becomes an
# immediate AttributeError instead of a silently-mis-typed event that
# slips into the jsonl and breaks the analyzer.
class EventType:
    COLLECTOR_START = "collector_start"
    COLLECTOR_STOP = "collector_stop"
    RSSI_SAMPLE = "rssi_sample"
    RSSI_BASELINE = "rssi_baseline"
    PW_NODE_SNAPSHOT = "pw_node_snapshot"
    PW_XRUN = "pw_xrun"
    BLUEZ_TRANSPORT_SNAPSHOT = "bluez_transport_snapshot"
    ROUTE_CREATE = "route_create"
    ROUTE_TEARDOWN = "route_teardown"
    SET_LATENCY_REQUEST = "set_latency_request"
    SET_VOLUME_REQUEST = "set_volume_request"
    BLUEZ_CONNECT = "bluez_connect"
    BLUEZ_DISCONNECT = "bluez_disconnect"
    MIC_SEGMENT_WRITTEN = "mic_segment_written"
    # Slice 3 System Coordinator: per-tick observation summary and any
    # actions taken (rate adjustment, system-wide hold, soft-mute).
    COORDINATOR_TICK = "coordinator_tick"
    COORDINATOR_RATE_ADJUST = "coordinator_rate_adjust"
    COORDINATOR_SYSTEM_HOLD = "coordinator_system_hold"
    COORDINATOR_SOFT_MUTE = "coordinator_soft_mute"
    # Slice 4 mic-driven calibration. One event per phase boundary
    # (started, capturing, analyzing, applying, applied/failed) so
    # the analyzer can replay the entire calibration trajectory.
    CALIBRATION_RESULT = "calibration_result"


def telemetry_root() -> Path:
    """Return the configured TELEMETRY_ROOT, creating the dir tree if needed.

    Defaults to /home/syncsonic/syncsonic-telemetry/. Overridable via
    SYNCSONIC_TELEMETRY_ROOT for local development.
    """
    raw = os.environ.get("SYNCSONIC_TELEMETRY_ROOT", "/home/syncsonic/syncsonic-telemetry")
    root = Path(raw).expanduser()
    (root / "events").mkdir(parents=True, exist_ok=True)
    (root / "mic").mkdir(parents=True, exist_ok=True)
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    return root


__all__ = ["SCHEMA_VERSION", "EventType", "telemetry_root"]
