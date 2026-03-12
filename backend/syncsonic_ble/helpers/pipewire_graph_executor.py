from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Dict, Any, List

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

DSP_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
GRAPH_LAUNCH_STATE_PATH = os.path.join(DSP_DIR, "graph_launch_state.json")
GRAPH_EXECUTOR_DIR = os.path.join(DSP_DIR, "graph_executor_specs")
GRAPH_EXECUTOR_STATE_PATH = os.path.join(DSP_DIR, "graph_executor_state.json")
METADATA_NAME = "default"
METADATA_ID = "0"
METADATA_KEY_PREFIX = "syncsonic.dsp.exec."
POLL_INTERVAL_SEC = 0.25


class PipeWireGraphExecutor:
    """Converts launch manifests into concrete executable command plans.

    This is the last runtime contract before direct graph mutation:
    - reads graph launch manifests
    - renders stable command sequences for load/link/update operations
    - writes per-output and global executor specs
    - publishes executor plans to PipeWire metadata

    The actual live mutator can later consume these command plans directly.
    """

    def __init__(self) -> None:
        self._last_specs: Dict[str, Dict[str, Any]] = {}

    def run_forever(self) -> None:
        log.info("PipeWire graph executor started")
        while True:
            try:
                self._tick()
            except Exception as exc:
                log.exception("PipeWire graph executor tick failed: %s", exc)
            time.sleep(POLL_INTERVAL_SEC)

    def _tick(self) -> None:
        state = self._read_launch_state()
        outputs = state.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}

        os.makedirs(GRAPH_EXECUTOR_DIR, exist_ok=True)
        specs: Dict[str, Dict[str, Any]] = {}
        for mac, plan in outputs.items():
            if not isinstance(plan, dict):
                continue
            mac = mac.upper()
            spec = self._build_executor_spec(mac, plan)
            specs[mac] = spec
            self._write_per_output_spec(mac, spec)
            self._publish_metadata(mac, spec)

        for mac in list(self._last_specs.keys()):
            if mac not in specs:
                self._remove_per_output_spec(mac)
                self._delete_metadata(mac)

        self._write_state(specs)
        self._last_specs = specs

    def _build_executor_spec(self, mac: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        loader = plan.get("loader", {})
        if not isinstance(loader, dict):
            loader = {}
        links = plan.get("links", [])
        if not isinstance(links, list):
            links = []

        module_name = str(loader.get("module_name", "libpipewire-module-filter-chain"))
        module_args = loader.get("module_args", {})
        command_plan: List[Dict[str, Any]] = [
            {
                "op": "ensure_module",
                "resource_key": f"filter-chain:{mac}",
                "tool": "pw-cli",
                "args": [
                    "load-module",
                    module_name,
                    json.dumps(module_args, separators=(",", ":"), sort_keys=True),
                ],
            }
        ]
        for link in links:
            if not isinstance(link, dict):
                continue
            command_plan.append(
                {
                    "op": "ensure_link",
                    "tool": "pw-link",
                    "args": [
                        "-L",
                        str(link.get("from", "")),
                        str(link.get("to", "")),
                    ],
                }
            )

        return {
            "mac": mac,
            "executor_name": f"syncsonic-exec-{mac.replace(':', '_').lower()}",
            "strategy": "declarative-graph-apply",
            "preconditions": {
                "requires_pw_cli": True,
                "requires_pw_link": True,
                "requires_pipewire_metadata": True,
            },
            "commands": command_plan,
            "source_plan": plan,
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
        tmp_path = f"{GRAPH_EXECUTOR_STATE_PATH}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, GRAPH_EXECUTOR_STATE_PATH)

    def _spec_path(self, mac: str) -> str:
        return os.path.join(GRAPH_EXECUTOR_DIR, f"{mac.replace(':', '_').lower()}.json")

    def _read_launch_state(self) -> Dict[str, Any]:
        if not os.path.exists(GRAPH_LAUNCH_STATE_PATH):
            return {"schema": 1, "outputs": {}}
        try:
            with open(GRAPH_LAUNCH_STATE_PATH, "r", encoding="ascii") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                return {"schema": 1, "outputs": {}}
            state.setdefault("outputs", {})
            return state
        except Exception as exc:
            log.warning("Failed to read graph launch state: %s", exc)
            return {"schema": 1, "outputs": {}}


def main() -> None:
    executor = PipeWireGraphExecutor()
    executor.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
