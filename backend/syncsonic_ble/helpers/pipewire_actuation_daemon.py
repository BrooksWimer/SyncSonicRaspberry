from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Dict, Any

from syncsonic_ble.helpers.alignment_actuator import AlignmentActuatorEngine
from syncsonic_ble.helpers.pipewire_alignment_maintenance import PipeWireAlignmentMaintenance
from syncsonic_ble.helpers.pipewire_control_plane import read_control_state
from syncsonic_ble.helpers.pipewire_dsp_contract import write_dsp_state
from syncsonic_ble.helpers.pipewire_observability import PipeWireObservability
from syncsonic_ble.helpers.pipewire_profiler_monitor import PipeWireProfilerMonitor
from syncsonic_ble.helpers.pipewire_transport import get_pipewire_transport_manager
from syncsonic_ble.helpers.device_labels import format_device_label
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

METADATA_NAME = "default"
METADATA_ID = "0"
METADATA_KEY_PREFIX = "syncsonic.output."
HEALTH_METADATA_KEY_PREFIX = "syncsonic.output.health."
OBS_METADATA_KEY_PREFIX = "syncsonic.output.obs."
OBS_EVENT_METADATA_KEY_PREFIX = "syncsonic.output.event."
POLL_INTERVAL_SEC = 0.25
CONTROL_LOG_HEARTBEAT_SEC = float(os.environ.get("SYNCSONIC_CTRL_LOG_HEARTBEAT_SEC", "0"))


class PipeWireActuationDaemon:
    """Persistent PipeWire-side runtime for SyncSonic output actuation.

    Responsibilities:
    - watch the SyncSonic control-plane state file
    - keep one native pw-loopback transport per active output
    - publish per-output delay/rate targets into PipeWire metadata

    This is the PipeWire-native runtime side of the redesign. SyncSonic writes
    control targets; this daemon owns the long-lived graph/runtime machinery.
    """

    def __init__(self) -> None:
        self._engine = AlignmentActuatorEngine()
        self._maintenance = PipeWireAlignmentMaintenance()
        self._observability = PipeWireObservability()
        self._profiler = PipeWireProfilerMonitor()
        self._transport = get_pipewire_transport_manager()
        self._last_outputs: Dict[str, Dict[str, Any]] = {}
        self._last_health: Dict[str, Dict[str, Any]] = {}
        self._route_failures: Dict[str, int] = {}
        self._last_control_log_signature: Dict[str, tuple] = {}
        self._last_control_log_ts: Dict[str, float] = {}

    def run_forever(self) -> None:
        log.info("PipeWire actuation daemon started")
        while True:
            try:
                self._tick()
            except Exception as exc:
                log.exception("PipeWire actuation daemon tick failed: %s", exc)
            time.sleep(POLL_INTERVAL_SEC)

    def _tick(self) -> None:
        state = read_control_state()
        outputs = state.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}

        current: Dict[str, Dict[str, Any]] = {}
        for mac, raw in outputs.items():
            if not isinstance(raw, dict):
                continue
            current[mac.upper()] = raw

        globals_state = state.get("globals", {})
        if not isinstance(globals_state, dict):
            globals_state = {}
        transport_base_ms = float(globals_state.get("transport_base_ms", 120.0))

        adjusted_targets, health_snapshots = self._maintenance.apply(
            current,
            transport_base_ms=transport_base_ms,
        )
        profiler_snapshot = self._profiler.sample()
        applied_states = self._engine.step(adjusted_targets, transport_base_ms=transport_base_ms)
        dsp_outputs: Dict[str, Dict[str, Any]] = {}

        for mac, cfg in adjusted_targets.items():
            active = bool(cfg.get("active", True))
            if not active:
                self._transport.remove_route(mac)
                self._delete_metadata(mac, applied=False)
                self._delete_metadata(mac, applied=True)
                self._delete_health_metadata(mac)
                self._route_failures.pop(mac, None)
                continue

            applied = applied_states.get(mac, {})
            health = health_snapshots.get(mac, {})
            stage_delay_ms = float(applied.get("delay_line_ms", 0.0))
            left_percent = cfg.get("left_percent")
            right_percent = cfg.get("right_percent")
            route_ok = self._transport.ensure_route(
                mac,
                latency_ms=stage_delay_ms,
                left_percent=int(left_percent) if left_percent is not None else None,
                right_percent=int(right_percent) if right_percent is not None else None,
            )
            route_failures = self._route_failures.get(mac, 0)
            if route_ok:
                route_failures = 0
            else:
                route_failures += 1
                log.warning("PipeWire transport ensure_route failed for %s (%d)", format_device_label(mac), route_failures)
            self._route_failures[mac] = route_failures

            health_view = dict(health)
            if not route_ok:
                health_view["health_state"] = "route_unavailable"
                health_view["remeasure_required"] = True
                health_view["confidence"] = min(float(health_view.get("confidence", 0.0)), 0.15)

            transport_delay_ms = float(applied.get("transport_delay_ms", 120.0))
            dsp_callback = self._transport.get_dsp_telemetry_snapshot(mac)
            profile = profiler_snapshot.get(mac, {}) if isinstance(profiler_snapshot, dict) else {}
            dsp_outputs[mac] = {
                "mode": str(applied.get("mode", cfg.get("mode", "idle"))),
                "transport_delay_ms": round(transport_delay_ms, 3),
                "delay_line_ms": round(float(applied.get("delay_line_ms", 0.0)), 3),
                "applied_rate_ppm": round(float(applied.get("applied_rate_ppm", cfg.get("rate_ppm", 0.0))), 3),
                "target_delay_ms": round(float(cfg.get("delay_ms", 100.0)), 3),
                "target_rate_ppm": round(float(cfg.get("rate_ppm", 0.0)), 3),
                "relock_events": int(applied.get("relock_events", 0)),
                "correction_events": int(applied.get("correction_events", 0)),
                "health_state": str(health_view.get("health_state", "unknown")),
                "alignment_confidence": round(float(health_view.get("confidence", 0.0)), 3),
                "baseline_latency_ms": (
                    round(float(health_view.get("baseline_latency_ms")), 3)
                    if health_view.get("baseline_latency_ms") is not None
                    else None
                ),
                "observed_latency_ms": (
                    round(float(health_view.get("last_observed_latency_ms")), 3)
                    if health_view.get("last_observed_latency_ms") is not None
                    else None
                ),
                "auto_trim_ms": round(float(health_view.get("auto_trim_ms", 0.0)), 3),
                "drift_observed_ms": round(float(health_view.get("drift_ms", 0.0)), 3),
                "dropout_events": int(health_view.get("dropout_events", 0)),
                "reconnect_events": int(health_view.get("reconnect_events", 0)),
                "remeasure_required": bool(health_view.get("remeasure_required", False)),
                "route_ok": bool(route_ok),
                "route_failure_count": int(route_failures),
                "dsp_callback": dsp_callback,
                "profiler": profile,
            }
            self._publish_metadata(
                mac,
                delay_ms=float(cfg.get("delay_ms", 100.0)),
                rate_ppm=float(cfg.get("rate_ppm", 0.0)),
                mode=str(cfg.get("mode", "idle")),
                applied=False,
            )
            self._publish_metadata(
                mac,
                delay_ms=float(applied.get("applied_delay_ms", cfg.get("delay_ms", 100.0))),
                rate_ppm=float(applied.get("applied_rate_ppm", cfg.get("rate_ppm", 0.0))),
                mode=str(applied.get("mode", cfg.get("mode", "idle"))),
                applied=True,
            )
            self._publish_health_metadata(mac, health=health_view)
            self._observability.observe_output(
                mac,
                route_ok=route_ok,
                route_failure_count=route_failures,
                health=health_view,
                applied=applied,
            )
            obs_snapshot = self._observability.snapshot(mac)
            latest_event = self._observability.latest_event(mac)
            if latest_event:
                dsp_outputs[mac]["last_event_type"] = str(latest_event.get("type", ""))
                dsp_outputs[mac]["last_event_severity"] = str(latest_event.get("severity", ""))
                dsp_outputs[mac]["last_event_at_unix"] = float(latest_event.get("at_unix", 0.0))
            self._publish_observability_metadata(mac, snapshot=obs_snapshot)
            self._publish_latest_event_metadata(mac, latest_event=latest_event)
            self._log_control_levers(mac, cfg=cfg, applied=applied, health=health_view, route_ok=route_ok, route_failures=route_failures)

        for mac in list(self._last_outputs.keys()):
            if mac not in adjusted_targets:
                self._transport.remove_route(mac)
                self._delete_metadata(mac, applied=False)
                self._delete_metadata(mac, applied=True)
                self._delete_health_metadata(mac)
                self._delete_observability_metadata(mac)
                self._delete_latest_event_metadata(mac)
                self._route_failures.pop(mac, None)
                self._last_control_log_signature.pop(mac, None)
                self._last_control_log_ts.pop(mac, None)
                self._observability.remove_output(mac)

        write_dsp_state(dsp_outputs)
        self._last_outputs = adjusted_targets
        self._last_health = health_snapshots

    def _publish_metadata(self, mac: str, *, delay_ms: float, rate_ppm: float, mode: str, applied: bool) -> None:
        key = f"{METADATA_KEY_PREFIX}{'applied.' if applied else ''}{mac}"
        value = json.dumps(
            {
                "delay_ms": round(delay_ms, 3),
                "rate_ppm": round(rate_ppm, 3),
                "mode": mode,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, METADATA_ID, key, value, "Spa:String:JSON"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _delete_metadata(self, mac: str, *, applied: bool) -> None:
        key = f"{METADATA_KEY_PREFIX}{'applied.' if applied else ''}{mac}"
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, "-d", METADATA_ID, key],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _publish_health_metadata(self, mac: str, *, health: Dict[str, Any]) -> None:
        key = f"{HEALTH_METADATA_KEY_PREFIX}{mac}"
        value = json.dumps(health, separators=(",", ":"), sort_keys=True)
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, METADATA_ID, key, value, "Spa:String:JSON"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _delete_health_metadata(self, mac: str) -> None:
        key = f"{HEALTH_METADATA_KEY_PREFIX}{mac}"
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, "-d", METADATA_ID, key],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _publish_observability_metadata(self, mac: str, *, snapshot: Dict[str, Any]) -> None:
        key = f"{OBS_METADATA_KEY_PREFIX}{mac}"
        value = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, METADATA_ID, key, value, "Spa:String:JSON"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _delete_observability_metadata(self, mac: str) -> None:
        key = f"{OBS_METADATA_KEY_PREFIX}{mac}"
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, "-d", METADATA_ID, key],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _publish_latest_event_metadata(self, mac: str, *, latest_event: Dict[str, Any]) -> None:
        key = f"{OBS_EVENT_METADATA_KEY_PREFIX}{mac}"
        value = json.dumps(latest_event, separators=(",", ":"), sort_keys=True)
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, METADATA_ID, key, value, "Spa:String:JSON"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _delete_latest_event_metadata(self, mac: str) -> None:
        key = f"{OBS_EVENT_METADATA_KEY_PREFIX}{mac}"
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, "-d", METADATA_ID, key],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _log_control_levers(
        self,
        mac: str,
        *,
        cfg: Dict[str, Any],
        applied: Dict[str, Any],
        health: Dict[str, Any],
        route_ok: bool,
        route_failures: int,
    ) -> None:
        now = time.monotonic()
        signature = (
            round(float(cfg.get("delay_ms", 0.0)), 1),
            round(float(applied.get("applied_delay_ms", cfg.get("delay_ms", 0.0))), 1),
            round(float(cfg.get("rate_ppm", 0.0)), 1),
            round(float(applied.get("applied_rate_ppm", cfg.get("rate_ppm", 0.0))), 1),
            round(float(health.get("drift_ms", 0.0)), 1),
            round(float(health.get("auto_trim_ms", 0.0)), 1),
            round(float(health.get("confidence", 0.0)), 2),
            str(health.get("health_state", "unknown")),
            bool(route_ok),
            int(route_failures),
        )
        last = self._last_control_log_signature.get(mac)
        last_ts = self._last_control_log_ts.get(mac, 0.0)
        if last == signature:
            if CONTROL_LOG_HEARTBEAT_SEC <= 0.0:
                return
            if (now - last_ts) < CONTROL_LOG_HEARTBEAT_SEC:
                return
        self._last_control_log_signature[mac] = signature
        self._last_control_log_ts[mac] = now
        log.info(
            "[CTRL] speaker=%s mode=%s target_delay=%.1fms applied_delay=%.1fms "
            "target_rate=%.1fppm applied_rate=%.1fppm drift=%.1fms trim=%.1fms "
            "conf=%.2f health=%s route_ok=%s route_failures=%d remeasure=%s",
            format_device_label(mac),
            cfg.get("mode", "idle"),
            float(cfg.get("delay_ms", 0.0)),
            float(applied.get("applied_delay_ms", cfg.get("delay_ms", 0.0))),
            float(cfg.get("rate_ppm", 0.0)),
            float(applied.get("applied_rate_ppm", cfg.get("rate_ppm", 0.0))),
            float(health.get("drift_ms", 0.0)),
            float(health.get("auto_trim_ms", 0.0)),
            float(health.get("confidence", 0.0)),
            str(health.get("health_state", "unknown")),
            bool(route_ok),
            int(route_failures),
            bool(health.get("remeasure_required", False)),
        )


def main() -> None:
    daemon = PipeWireActuationDaemon()
    daemon.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
