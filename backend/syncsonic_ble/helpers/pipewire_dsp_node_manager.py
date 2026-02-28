from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Dict, Any

from syncsonic_ble.helpers.pipewire_transport import resolve_pipewire_output_name
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

DSP_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
DSP_RENDER_STATE_PATH = os.path.join(DSP_DIR, "dsp_render_state.json")
DSP_NODE_STATE_PATH = os.path.join(DSP_DIR, "dsp_node_state.json")
METADATA_NAME = "default"
METADATA_ID = "0"
METADATA_KEY_PREFIX = "syncsonic.dsp.node."
POLL_INTERVAL_SEC = 0.25
SAMPLE_RATE = 48_000.0


class PipeWireDspNodeManager:
    """Renders per-output native DSP node specs from the DSP runtime state.

    This is the final control-side consumer before a true DSP node implementation:
    - reads `dsp_render_state.json`
    - converts each output into a concrete delay/resampler node spec
    - publishes that spec via PipeWire metadata
    - mirrors the active node spec set to `dsp_node_state.json`

    The actual custom DSP node can bind to this stable node spec contract.
    """

    def __init__(self) -> None:
        self._last_specs: Dict[str, Dict[str, Any]] = {}

    def run_forever(self) -> None:
        log.info("PipeWire DSP node manager started")
        while True:
            try:
                self._tick()
            except Exception as exc:
                log.exception("PipeWire DSP node manager tick failed: %s", exc)
            time.sleep(POLL_INTERVAL_SEC)

    def _tick(self) -> None:
        state = self._read_render_state()
        outputs = state.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}

        specs: Dict[str, Dict[str, Any]] = {}
        for mac, rendered in outputs.items():
            if not isinstance(rendered, dict):
                continue
            mac = mac.upper()
            spec = self._build_node_spec(mac, rendered)
            specs[mac] = spec
            self._publish_metadata(mac, spec)

        for mac in list(self._last_specs.keys()):
            if mac not in specs:
                self._delete_metadata(mac)

        self._write_node_state(specs)
        self._last_specs = specs

    def _build_node_spec(self, mac: str, rendered: Dict[str, Any]) -> Dict[str, Any]:
        delay_line_ms = max(0.0, float(rendered.get("delay_line_ms", 0.0)))
        delay_samples = int(round(delay_line_ms * SAMPLE_RATE / 1000.0))
        resampler_ratio = float(rendered.get("resampler_ratio", 1.0))
        safe_ratio = max(0.999, min(1.001, resampler_ratio))
        target_output = resolve_pipewire_output_name(mac) or f"bluez_output.{mac.replace(':', '_')}"
        return {
            "mac": mac,
            "node_name": f"syncsonic-dsp-{mac.replace(':', '_').lower()}",
            "capture_node": "virtual_out.monitor",
            "target_sink_hint": target_output,
            "mode": str(rendered.get("mode", "idle")),
            "delay_line_ms": round(delay_line_ms, 3),
            "delay_line_samples": delay_samples,
            "resampler_ratio": round(safe_ratio, 9),
            "applied_rate_ppm": round(float(rendered.get("applied_rate_ppm", 0.0)), 3),
            "transport_delay_ms": round(float(rendered.get("transport_delay_ms", 120.0)), 3),
            "filter_chain": {
                "type": "syncsonic-delay-resampler-v1",
                "delay_seconds": round(delay_line_ms / 1000.0, 6),
                "resample_ratio": round(safe_ratio, 9),
                "channels": 2,
                "sample_rate": int(SAMPLE_RATE),
            },
            "telemetry": {
                "target_delay_ms": round(float(rendered.get("target_delay_ms", 0.0)), 3),
                "target_rate_ppm": round(float(rendered.get("target_rate_ppm", 0.0)), 3),
                "relock_events": int(rendered.get("relock_events", 0)),
                "correction_events": int(rendered.get("correction_events", 0)),
            },
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

    def _read_render_state(self) -> Dict[str, Any]:
        if not os.path.exists(DSP_RENDER_STATE_PATH):
            return {"schema": 1, "outputs": {}}
        try:
            with open(DSP_RENDER_STATE_PATH, "r", encoding="ascii") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                return {"schema": 1, "outputs": {}}
            state.setdefault("outputs", {})
            return state
        except Exception as exc:
            log.warning("Failed to read DSP render state: %s", exc)
            return {"schema": 1, "outputs": {}}

    def _write_node_state(self, specs: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(DSP_DIR, exist_ok=True)
        payload = {"schema": 1, "outputs": specs}
        tmp_path = f"{DSP_NODE_STATE_PATH}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, DSP_NODE_STATE_PATH)


def main() -> None:
    manager = PipeWireDspNodeManager()
    manager.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
