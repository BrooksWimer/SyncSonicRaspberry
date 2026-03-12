from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

from syncsonic_ble.helpers.device_labels import format_device_label
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

OBS_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
OBS_STATE_PATH = os.path.join(OBS_DIR, "observability_state.json")
OBS_EVENT_PATH = os.path.join(OBS_DIR, "observability_events.json")


@dataclass
class OutputObservabilityState:
    mac: str
    route_ok: bool = True
    sink_state: str = "UNKNOWN"
    telemetry_present: bool = False
    observed_latency_ms: Optional[float] = None
    alignment_confidence: float = 0.0
    health_state: str = "unknown"
    last_update_unix: float = 0.0


class PipeWireObservability:
    """Tracks output runtime anomalies and emits structured events.

    This is intentionally detection-only. It does not mutate correction targets.
    """

    LATENCY_JUMP_MS = float(os.environ.get("SYNCSONIC_OBS_LATENCY_JUMP_MS", "20.0"))
    LOW_CONFIDENCE_THRESHOLD = float(os.environ.get("SYNCSONIC_OBS_LOW_CONF", "0.2"))
    MAX_EVENT_HISTORY = int(os.environ.get("SYNCSONIC_OBS_MAX_EVENTS", "300"))

    def __init__(self) -> None:
        self._states: Dict[str, OutputObservabilityState] = {}
        self._active_incidents: Dict[Tuple[str, str], float] = {}
        self._recent_events: List[Dict[str, Any]] = []

    def observe_output(
        self,
        mac: str,
        *,
        route_ok: bool,
        route_failure_count: int,
        health: Dict[str, Any],
        applied: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        now = time.time()
        mac = mac.upper()
        previous = self._states.get(mac)

        sink_state = str(health.get("last_sink_state", "UNKNOWN")).upper()
        observed_latency_raw = health.get("last_observed_latency_ms")
        observed_latency_ms = float(observed_latency_raw) if observed_latency_raw is not None else None
        confidence = float(health.get("confidence", 0.0))
        health_state = str(health.get("health_state", "unknown"))
        telemetry_present = observed_latency_ms is not None

        current = OutputObservabilityState(
            mac=mac,
            route_ok=bool(route_ok),
            sink_state=sink_state,
            telemetry_present=telemetry_present,
            observed_latency_ms=observed_latency_ms,
            alignment_confidence=confidence,
            health_state=health_state,
            last_update_unix=now,
        )
        self._states[mac] = current

        events: List[Dict[str, Any]] = []
        if previous is None:
            events.append(
                self._event(
                    "output_observed",
                    mac,
                    "info",
                    {
                        "route_ok": route_ok,
                        "sink_state": sink_state,
                        "telemetry_present": telemetry_present,
                        "confidence": round(confidence, 3),
                    },
                )
            )
            self._record_events(events)
            self._write_state()
            return events

        if previous.route_ok and not current.route_ok:
            self._active_incidents[(mac, "route")] = now
            events.append(
                self._event(
                    "route_lost",
                    mac,
                    "warning",
                    {
                        "route_failure_count": int(route_failure_count),
                        "health_state": health_state,
                    },
                )
            )
        elif (not previous.route_ok) and current.route_ok:
            started_at = self._active_incidents.pop((mac, "route"), None)
            duration_ms = ((now - started_at) * 1000.0) if started_at else None
            events.append(
                self._event(
                    "route_recovered",
                    mac,
                    "info",
                    {
                        "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
                        "route_failure_count": int(route_failure_count),
                    },
                )
            )

        if previous.telemetry_present and not current.telemetry_present:
            self._active_incidents[(mac, "telemetry")] = now
            events.append(
                self._event(
                    "telemetry_lost",
                    mac,
                    "warning",
                    {
                        "sink_state": sink_state,
                        "health_state": health_state,
                    },
                )
            )
        elif (not previous.telemetry_present) and current.telemetry_present:
            started_at = self._active_incidents.pop((mac, "telemetry"), None)
            duration_ms = ((now - started_at) * 1000.0) if started_at else None
            events.append(
                self._event(
                    "telemetry_recovered",
                    mac,
                    "info",
                    {
                        "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
                        "observed_latency_ms": round(float(observed_latency_ms), 3),
                    },
                )
            )

        if previous.sink_state != current.sink_state:
            events.append(
                self._event(
                    "sink_state_changed",
                    mac,
                    "info",
                    {
                        "from": previous.sink_state,
                        "to": current.sink_state,
                    },
                )
            )

        if previous.observed_latency_ms is not None and current.observed_latency_ms is not None:
            latency_delta = current.observed_latency_ms - previous.observed_latency_ms
            if abs(latency_delta) >= self.LATENCY_JUMP_MS:
                events.append(
                    self._event(
                        "latency_jump",
                        mac,
                        "warning",
                        {
                            "delta_ms": round(latency_delta, 3),
                            "from_ms": round(previous.observed_latency_ms, 3),
                            "to_ms": round(current.observed_latency_ms, 3),
                            "target_delay_ms": round(float(applied.get("applied_delay_ms", 0.0)), 3),
                        },
                    )
                )

        if (
            previous.alignment_confidence > self.LOW_CONFIDENCE_THRESHOLD
            and current.alignment_confidence <= self.LOW_CONFIDENCE_THRESHOLD
        ):
            events.append(
                self._event(
                    "low_confidence",
                    mac,
                    "warning",
                    {
                        "confidence": round(current.alignment_confidence, 3),
                        "health_state": current.health_state,
                    },
                )
            )

        self._record_events(events)
        self._write_state()
        return events

    def remove_output(self, mac: str) -> List[Dict[str, Any]]:
        now = time.time()
        mac = mac.upper()
        events: List[Dict[str, Any]] = []
        if mac in self._states:
            self._states.pop(mac, None)
            self._active_incidents.pop((mac, "route"), None)
            self._active_incidents.pop((mac, "telemetry"), None)
            events.append(self._event("output_removed", mac, "info", {"removed_at_unix": round(now, 3)}))
            self._record_events(events)
            self._write_state()
        return events

    def snapshot(self, mac: Optional[str] = None) -> Dict[str, Any]:
        if mac is not None:
            state = self._states.get(mac.upper())
            return asdict(state) if state else {}
        return {key: asdict(value) for key, value in self._states.items()}

    def latest_event(self, mac: str) -> Dict[str, Any]:
        mac = mac.upper()
        for event in reversed(self._recent_events):
            if event.get("mac") == mac:
                return event
        return {}

    def _event(self, event_type: str, mac: str, severity: str, details: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": event_type,
            "mac": mac.upper(),
            "severity": severity,
            "at_unix": round(time.time(), 3),
            "details": details,
        }

    def _record_events(self, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        for event in events:
            self._recent_events.append(event)
            if event["severity"] == "warning":
                log.warning("[OBS] %s speaker=%s details=%s", event["type"], format_device_label(str(event["mac"])), event["details"])
            else:
                log.info("[OBS] %s speaker=%s details=%s", event["type"], format_device_label(str(event["mac"])), event["details"])
        if len(self._recent_events) > self.MAX_EVENT_HISTORY:
            self._recent_events = self._recent_events[-self.MAX_EVENT_HISTORY :]
        self._write_event_log()

    def _write_state(self) -> None:
        os.makedirs(OBS_DIR, exist_ok=True)
        payload = {
            "schema": 1,
            "outputs": self.snapshot(),
            "active_incidents": {f"{mac}:{kind}": round(ts, 3) for (mac, kind), ts in self._active_incidents.items()},
            "updated_at_unix": round(time.time(), 3),
        }
        tmp_path = f"{OBS_STATE_PATH}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, OBS_STATE_PATH)

    def _write_event_log(self) -> None:
        os.makedirs(OBS_DIR, exist_ok=True)
        payload = {
            "schema": 1,
            "events": self._recent_events[-self.MAX_EVENT_HISTORY :],
        }
        tmp_path = f"{OBS_EVENT_PATH}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, OBS_EVENT_PATH)
