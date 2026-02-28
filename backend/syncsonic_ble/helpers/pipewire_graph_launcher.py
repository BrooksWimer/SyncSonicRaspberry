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
FILTER_CHAIN_STATE_PATH = os.path.join(DSP_DIR, "filter_chain_state.json")
GRAPH_LAUNCH_DIR = os.path.join(DSP_DIR, "graph_launch_specs")
GRAPH_LAUNCH_STATE_PATH = os.path.join(DSP_DIR, "graph_launch_state.json")
METADATA_NAME = "default"
METADATA_ID = "0"
METADATA_KEY_PREFIX = "syncsonic.dsp.launch."
POLL_INTERVAL_SEC = 0.25


class PipeWireGraphLauncher:
    """Builds concrete graph launch plans from filter-chain specs.

    This is the final orchestration contract before an actual PipeWire loader
    applies the graph changes. It emits one launch manifest per output and a
    global state file, plus publishes the manifest over PipeWire metadata.
    """

    def __init__(self) -> None:
        self._last_plans: Dict[str, Dict[str, Any]] = {}

    def run_forever(self) -> None:
        log.info("PipeWire graph launcher started")
        while True:
            try:
                self._tick()
            except Exception as exc:
                log.exception("PipeWire graph launcher tick failed: %s", exc)
            time.sleep(POLL_INTERVAL_SEC)

    def _tick(self) -> None:
        state = self._read_filter_chain_state()
        outputs = state.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}

        os.makedirs(GRAPH_LAUNCH_DIR, exist_ok=True)
        plans: Dict[str, Dict[str, Any]] = {}
        for mac, spec in outputs.items():
            if not isinstance(spec, dict):
                continue
            mac = mac.upper()
            plan = self._build_launch_plan(mac, spec)
            plans[mac] = plan
            self._write_per_output_plan(mac, plan)
            self._publish_metadata(mac, plan)

        for mac in list(self._last_plans.keys()):
            if mac not in plans:
                self._remove_per_output_plan(mac)
                self._delete_metadata(mac)

        self._write_state(plans)
        self._last_plans = plans

    def _build_launch_plan(self, mac: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        graph = spec.get("graph", {})
        if not isinstance(graph, dict):
            graph = {}
        stages = graph.get("stages", [])
        if not isinstance(stages, list):
            stages = []

        node_name = str(spec.get("node_name", f"syncsonic-processor-{mac.replace(':', '_').lower()}"))
        capture_node = str(spec.get("capture_node", "virtual_out.monitor"))
        target_sink_hint = str(spec.get("target_sink_hint", ""))

        module_args = {
            "node.name": node_name,
            "node.description": spec.get("description", node_name),
            "capture.props": {
                "target.object": capture_node,
                "node.passive": True,
            },
            "playback.props": {
                "target.object": target_sink_hint,
                "node.passive": True,
            },
            "filter.graph": {
                "nodes": stages,
            },
        }

        return {
            "mac": mac,
            "node_name": node_name,
            "loader": {
                "type": "pipewire-module-filter-chain",
                "module_name": "libpipewire-module-filter-chain",
                "module_args": module_args,
            },
            "links": [],
            "apply_strategy": {
                "mode": "persistent",
                "reload_on_change": True,
                "linger_links": False,
            },
            "source_spec": spec,
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

    def _write_per_output_plan(self, mac: str, plan: Dict[str, Any]) -> None:
        path = self._plan_path(mac)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(plan, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, path)

    def _remove_per_output_plan(self, mac: str) -> None:
        path = self._plan_path(mac)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _write_state(self, plans: Dict[str, Dict[str, Any]]) -> None:
        payload = {"schema": 1, "outputs": plans}
        tmp_path = f"{GRAPH_LAUNCH_STATE_PATH}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, GRAPH_LAUNCH_STATE_PATH)

    def _plan_path(self, mac: str) -> str:
        return os.path.join(GRAPH_LAUNCH_DIR, f"{mac.replace(':', '_').lower()}.json")

    def _read_filter_chain_state(self) -> Dict[str, Any]:
        if not os.path.exists(FILTER_CHAIN_STATE_PATH):
            return {"schema": 1, "outputs": {}}
        try:
            with open(FILTER_CHAIN_STATE_PATH, "r", encoding="ascii") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                return {"schema": 1, "outputs": {}}
            state.setdefault("outputs", {})
            return state
        except Exception as exc:
            log.warning("Failed to read filter chain state: %s", exc)
            return {"schema": 1, "outputs": {}}


def main() -> None:
    launcher = PipeWireGraphLauncher()
    launcher.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
