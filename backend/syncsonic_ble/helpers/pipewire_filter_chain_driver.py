from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from typing import Dict, Any

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

DSP_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
PROCESSOR_STATE_PATH = os.path.join(DSP_DIR, "processor_state.json")
FILTER_CHAIN_DIR = os.path.join(DSP_DIR, "filter_chain_specs")
FILTER_CHAIN_STATE_PATH = os.path.join(DSP_DIR, "filter_chain_state.json")
POLL_INTERVAL_SEC = 0.25


class PipeWireFilterChainDriver:
    """Renders concrete filter-chain specs from processor plans.

    This driver is the final binding layer before a real PipeWire custom node or
    loader consumes the spec. It writes one spec file per output plus a global
    state file that a launcher/plugin can ingest.
    """

    def __init__(self) -> None:
        self._last_specs: Dict[str, Dict[str, Any]] = {}

    def run_forever(self) -> None:
        log.info("PipeWire filter-chain driver started")
        while True:
            try:
                self._tick()
            except Exception as exc:
                log.exception("PipeWire filter-chain driver tick failed: %s", exc)
            time.sleep(POLL_INTERVAL_SEC)

    def _tick(self) -> None:
        state = self._read_processor_state()
        outputs = state.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}

        specs: Dict[str, Dict[str, Any]] = {}
        os.makedirs(FILTER_CHAIN_DIR, exist_ok=True)
        for mac, plan in outputs.items():
            if not isinstance(plan, dict):
                continue
            mac = mac.upper()
            spec = self._build_filter_chain_spec(mac, plan)
            specs[mac] = spec
            self._write_per_output_spec(mac, spec)

        for mac in list(self._last_specs.keys()):
            if mac not in specs:
                self._remove_per_output_spec(mac)

        self._write_state(specs)
        self._last_specs = specs

    def _build_filter_chain_spec(self, mac: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        controls = plan.get("controls", {})
        topology = plan.get("topology", {})
        if not isinstance(controls, dict):
            controls = {}
        if not isinstance(topology, dict):
            topology = {}
        delay_seconds = float(controls.get("delay_seconds", 0.0))
        resample_ratio = float(controls.get("resample_ratio", 1.0))
        sample_rate = int(topology.get("sample_rate", 48000))
        channels = int(topology.get("channels", 2))
        node_name = str(plan.get("processor_name", f"syncsonic-processor-{mac.replace(':', '_').lower()}"))
        return {
            "mac": mac,
            "node_name": node_name,
            "description": f"SyncSonic DSP processor for {mac}",
            "factory_name": "filter-chain",
            "supports_native_rate": False,
            "graph": {
                "sample_rate": sample_rate,
                "channels": channels,
                "stages": [
                    {
                        "name": "delay_line",
                        "type": "builtin",
                        "label": "delay",
                        "control": {
                            "Delay (s)": round(delay_seconds, 6),
                        },
                    },
                ],
            },
            "capture_node": plan.get("capture_node", "virtual_out.monitor"),
            "target_sink_hint": plan.get("target_sink_hint", ""),
            "telemetry": {
                **(plan.get("telemetry", {}) if isinstance(plan.get("telemetry", {}), dict) else {}),
                "rate_target_ratio": round(resample_ratio, 9),
                "rate_control_degraded": True,
            },
        }

    def _write_per_output_spec(self, mac: str, spec: Dict[str, Any]) -> None:
        path = self._spec_path(mac)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(spec, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, path)

    def _remove_per_output_spec(self, mac: str) -> None:
        path = self._spec_path(mac)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _write_state(self, specs: Dict[str, Dict[str, Any]]) -> None:
        payload = {"schema": 1, "outputs": specs}
        tmp_path = f"{FILTER_CHAIN_STATE_PATH}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, FILTER_CHAIN_STATE_PATH)

    def _spec_path(self, mac: str) -> str:
        return os.path.join(FILTER_CHAIN_DIR, f"{mac.replace(':', '_').lower()}.json")

    def _read_processor_state(self) -> Dict[str, Any]:
        if not os.path.exists(PROCESSOR_STATE_PATH):
            return {"schema": 1, "outputs": {}}
        try:
            with open(PROCESSOR_STATE_PATH, "r", encoding="ascii") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                return {"schema": 1, "outputs": {}}
            state.setdefault("outputs", {})
            return state
        except Exception as exc:
            log.warning("Failed to read processor state: %s", exc)
            return {"schema": 1, "outputs": {}}


def main() -> None:
    driver = PipeWireFilterChainDriver()
    driver.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
