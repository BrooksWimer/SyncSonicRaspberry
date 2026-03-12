from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Dict, Any

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

DSP_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
DSP_STATE_PATH = os.path.join(DSP_DIR, "dsp_state.json")
DSP_RENDER_STATE_PATH = os.path.join(DSP_DIR, "dsp_render_state.json")
METADATA_NAME = "default"
METADATA_ID = "0"
METADATA_KEY_PREFIX = "syncsonic.dsp."
POLL_INTERVAL_SEC = 0.25


class PipeWireDspRuntime:
    """Consumes SyncSonic DSP state and publishes a runtime-facing DSP spec.

    This is the native runtime bridge for the future owned DSP node:
    - reads the actuator-produced `dsp_state.json`
    - normalizes it into a per-output processing spec
    - publishes the spec into PipeWire metadata
    - mirrors the latest rendered spec to `dsp_render_state.json`

    The actual custom DSP node is the next piece, but this process becomes the
    stable contract boundary it will consume.
    """

    def __init__(self) -> None:
        self._last_outputs: Dict[str, Dict[str, Any]] = {}

    def run_forever(self) -> None:
        log.info("PipeWire DSP runtime started")
        while True:
            try:
                self._tick()
            except Exception as exc:
                log.exception("PipeWire DSP runtime tick failed: %s", exc)
            time.sleep(POLL_INTERVAL_SEC)

    def _tick(self) -> None:
        state = self._read_state()
        outputs = state.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}

        rendered: Dict[str, Dict[str, Any]] = {}
        for mac, raw in outputs.items():
            if not isinstance(raw, dict):
                continue
            mac = mac.upper()
            rendered[mac] = self._render_output(mac, raw)
            self._publish_metadata(mac, rendered[mac])

        for mac in list(self._last_outputs.keys()):
            if mac not in rendered:
                self._delete_metadata(mac)

        self._write_render_state(rendered)
        self._last_outputs = rendered

    def _render_output(self, mac: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        delay_line_ms = max(0.0, float(raw.get("delay_line_ms", 0.0)))
        applied_rate_ppm = float(raw.get("applied_rate_ppm", 0.0))
        baseline_latency_raw = raw.get("baseline_latency_ms")
        observed_latency_raw = raw.get("observed_latency_ms")
        return {
            "mac": mac,
            "mode": str(raw.get("mode", "idle")),
            "transport_delay_ms": round(float(raw.get("transport_delay_ms", 120.0)), 3),
            "delay_line_ms": round(delay_line_ms, 3),
            "resampler_ratio": round(1.0 + (applied_rate_ppm / 1_000_000.0), 9),
            "applied_rate_ppm": round(applied_rate_ppm, 3),
            "target_delay_ms": round(float(raw.get("target_delay_ms", 120.0)), 3),
            "target_rate_ppm": round(float(raw.get("target_rate_ppm", 0.0)), 3),
            "relock_events": int(raw.get("relock_events", 0)),
            "correction_events": int(raw.get("correction_events", 0)),
            "health_state": str(raw.get("health_state", "unknown")),
            "alignment_confidence": round(float(raw.get("alignment_confidence", 0.0)), 3),
            "baseline_latency_ms": (
                round(float(baseline_latency_raw), 3) if baseline_latency_raw is not None else None
            ),
            "observed_latency_ms": (
                round(float(observed_latency_raw), 3) if observed_latency_raw is not None else None
            ),
            "auto_trim_ms": round(float(raw.get("auto_trim_ms", 0.0)), 3),
            "drift_observed_ms": round(float(raw.get("drift_observed_ms", 0.0)), 3),
            "dropout_events": int(raw.get("dropout_events", 0)),
            "reconnect_events": int(raw.get("reconnect_events", 0)),
            "remeasure_required": bool(raw.get("remeasure_required", False)),
            "route_ok": bool(raw.get("route_ok", True)),
            "route_failure_count": int(raw.get("route_failure_count", 0)),
        }

    def _publish_metadata(self, mac: str, spec: Dict[str, Any]) -> None:
        key = f"{METADATA_KEY_PREFIX}{mac}"
        value = json.dumps(spec, separators=(",", ":"), sort_keys=True)
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, METADATA_ID, key, value, "Spa:String:JSON"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _delete_metadata(self, mac: str) -> None:
        key = f"{METADATA_KEY_PREFIX}{mac}"
        subprocess.run(
            ["pw-metadata", "-n", METADATA_NAME, "-d", METADATA_ID, key],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _read_state(self) -> Dict[str, Any]:
        if not os.path.exists(DSP_STATE_PATH):
            return {"schema": 1, "outputs": {}}
        try:
            with open(DSP_STATE_PATH, "r", encoding="ascii") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                return {"schema": 1, "outputs": {}}
            state.setdefault("outputs", {})
            return state
        except Exception as exc:
            log.warning("Failed to read DSP state: %s", exc)
            return {"schema": 1, "outputs": {}}

    def _write_render_state(self, outputs: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(DSP_DIR, exist_ok=True)
        payload = {"schema": 1, "outputs": outputs}
        tmp_path = f"{DSP_RENDER_STATE_PATH}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, DSP_RENDER_STATE_PATH)


def main() -> None:
    runtime = PipeWireDspRuntime()
    runtime.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
