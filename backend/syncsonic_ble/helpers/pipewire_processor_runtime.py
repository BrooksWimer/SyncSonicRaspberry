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
DSP_NODE_STATE_PATH = os.path.join(DSP_DIR, "dsp_node_state.json")
PROCESSOR_STATE_PATH = os.path.join(DSP_DIR, "processor_state.json")
METADATA_NAME = "default"
METADATA_ID = "0"
METADATA_KEY_PREFIX = "syncsonic.dsp.processor."
POLL_INTERVAL_SEC = 0.25


class PipeWireProcessorRuntime:
    """Builds concrete processor plans from DSP node specs.

    This is the final software contract before a custom native processor binds
    to the graph:
    - reads `dsp_node_state.json`
    - emits one processor plan per output
    - publishes those plans into PipeWire metadata
    - mirrors the active plans to `processor_state.json`

    Each plan describes the exact delay-line and resampler controls a future
    native node must expose.
    """

    def __init__(self) -> None:
        self._last_plans: Dict[str, Dict[str, Any]] = {}

    def run_forever(self) -> None:
        log.info("PipeWire processor runtime started")
        while True:
            try:
                self._tick()
            except Exception as exc:
                log.exception("PipeWire processor runtime tick failed: %s", exc)
            time.sleep(POLL_INTERVAL_SEC)

    def _tick(self) -> None:
        state = self._read_node_state()
        outputs = state.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}

        plans: Dict[str, Dict[str, Any]] = {}
        for mac, spec in outputs.items():
            if not isinstance(spec, dict):
                continue
            mac = mac.upper()
            plan = self._build_processor_plan(mac, spec)
            plans[mac] = plan
            self._publish_metadata(mac, plan)

        for mac in list(self._last_plans.keys()):
            if mac not in plans:
                self._delete_metadata(mac)

        self._write_state(plans)
        self._last_plans = plans

    def _build_processor_plan(self, mac: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        filter_chain = spec.get("filter_chain", {})
        if not isinstance(filter_chain, dict):
            filter_chain = {}
        delay_seconds = float(filter_chain.get("delay_seconds", 0.0))
        resample_ratio = float(filter_chain.get("resample_ratio", 1.0))
        return {
            "mac": mac,
            "processor_name": f"syncsonic-processor-{mac.replace(':', '_').lower()}",
            "node_name": spec.get("node_name", f"syncsonic-dsp-{mac.replace(':', '_').lower()}"),
            "factory_name": "support.null-audio-sink",
            "graph_role": "syncsonic-dsp-processor",
            "capture_node": spec.get("capture_node", "virtual_out.monitor"),
            "target_sink_hint": spec.get("target_sink_hint", ""),
            "mode": spec.get("mode", "idle"),
            "ports": {
                "audio_in": ["FL", "FR"],
                "audio_out": ["FL", "FR"],
                "control_in": ["delay_seconds", "resample_ratio"],
            },
            "controls": {
                "delay_seconds": round(delay_seconds, 6),
                "delay_samples": int(spec.get("delay_line_samples", 0)),
                "resample_ratio": round(resample_ratio, 9),
            },
            "topology": {
                "stages": [
                    {"type": "delay-line", "control": "delay_seconds"},
                    {"type": "variable-resampler", "control": "resample_ratio"},
                ],
                "channels": int(filter_chain.get("channels", 2)),
                "sample_rate": int(filter_chain.get("sample_rate", 48_000)),
            },
            "telemetry": spec.get("telemetry", {}),
        }

    def _publish_metadata(self, mac: str, plan: Dict[str, Any]) -> None:
        key = f"{METADATA_KEY_PREFIX}{mac}"
        value = json.dumps(plan, separators=(",", ":"), sort_keys=True)
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

    def _read_node_state(self) -> Dict[str, Any]:
        if not os.path.exists(DSP_NODE_STATE_PATH):
            return {"schema": 1, "outputs": {}}
        try:
            with open(DSP_NODE_STATE_PATH, "r", encoding="ascii") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                return {"schema": 1, "outputs": {}}
            state.setdefault("outputs", {})
            return state
        except Exception as exc:
            log.warning("Failed to read DSP node state: %s", exc)
            return {"schema": 1, "outputs": {}}

    def _write_state(self, plans: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(DSP_DIR, exist_ok=True)
        payload = {"schema": 1, "outputs": plans}
        tmp_path = f"{PROCESSOR_STATE_PATH}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, PROCESSOR_STATE_PATH)


def main() -> None:
    runtime = PipeWireProcessorRuntime()
    runtime.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
