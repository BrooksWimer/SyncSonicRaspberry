from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

DSP_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
GRAPH_EXECUTOR_STATE_PATH = os.path.join(DSP_DIR, "graph_executor_state.json")
GRAPH_APPLY_DIR = os.path.join(DSP_DIR, "graph_apply_specs")
GRAPH_APPLY_STATE_PATH = os.path.join(DSP_DIR, "graph_apply_state.json")
METADATA_NAME = "default"
METADATA_ID = "0"
METADATA_KEY_PREFIX = "syncsonic.dsp.apply."
POLL_INTERVAL_SEC = 0.25


class PipeWireGraphApplier:
    """Applies executor plans to the live PipeWire graph.

    This is the final runtime mutator in the current architecture:
    - reads graph executor plans
    - runs best-effort graph mutation commands when a plan changes
    - records applied state and command outcomes
    - publishes applied-state metadata for debugging
    """

    def __init__(self) -> None:
        self._last_plan_digests: Dict[str, str] = {}
        self._last_states: Dict[str, Dict[str, Any]] = {}

    def run_forever(self) -> None:
        log.info("PipeWire graph applier started")
        while True:
            try:
                self._tick()
            except Exception as exc:
                log.exception("PipeWire graph applier tick failed: %s", exc)
            time.sleep(POLL_INTERVAL_SEC)

    def _tick(self) -> None:
        state = self._read_executor_state()
        outputs = state.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}

        os.makedirs(GRAPH_APPLY_DIR, exist_ok=True)
        applied: Dict[str, Dict[str, Any]] = {}

        for mac, spec in outputs.items():
            if not isinstance(spec, dict):
                continue
            mac = mac.upper()
            plan_digest = self._digest_spec(spec)
            previous_digest = self._last_plan_digests.get(mac)
            previous_state = self._last_states.get(mac)

            if (
                previous_digest == plan_digest
                and previous_state is not None
                and bool(previous_state.get("success", False))
            ):
                applied_state = dict(previous_state)
                applied_state["unchanged"] = True
            else:
                if previous_state is not None and previous_digest != plan_digest:
                    self._teardown_previous(previous_state)
                applied_state = self._apply_plan(mac, spec, plan_digest)

            applied[mac] = applied_state
            self._write_per_output_state(mac, applied_state)
            self._publish_metadata(mac, applied_state)

        for mac in list(self._last_states.keys()):
            if mac not in applied:
                self._teardown_previous(self._last_states[mac])
                self._remove_per_output_state(mac)
                self._delete_metadata(mac)
                self._last_plan_digests.pop(mac, None)

        self._write_state(applied)
        self._last_states = applied

    def _apply_plan(self, mac: str, spec: Dict[str, Any], plan_digest: str) -> Dict[str, Any]:
        commands = spec.get("commands", [])
        if not isinstance(commands, list):
            commands = []

        outcomes: List[Dict[str, Any]] = []
        success = True
        changed = False
        resource_handles: Dict[str, Dict[str, Any]] = {}

        for command in commands:
            if not isinstance(command, dict):
                continue
            result = self._run_command(command)
            outcomes.append(result)
            changed = True
            resource_key = str(command.get("resource_key", "")).strip()
            if resource_key and result.get("resource_handle"):
                resource_handles[resource_key] = result["resource_handle"]
            if not result.get("ok", False):
                success = False

        state = {
            "mac": mac,
            "plan_digest": plan_digest,
            "executor_name": spec.get("executor_name"),
            "strategy": spec.get("strategy", "declarative-graph-apply"),
            "success": success,
            "changed": changed,
            "unchanged": False,
            "applied_at_unix": round(time.time(), 3),
            "tooling": {
                "pw_cli_found": bool(shutil.which("pw-cli")),
                "pw_link_found": bool(shutil.which("pw-link")),
                "pw_metadata_found": bool(shutil.which("pw-metadata")),
            },
            "resource_handles": resource_handles,
            "command_results": outcomes,
            "attempt_count": int(self._last_states.get(mac, {}).get("attempt_count", 0)) + 1,
            "source_spec": spec,
        }
        self._last_plan_digests[mac] = plan_digest
        return state

    def _run_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        tool = str(command.get("tool", "")).strip()
        args = command.get("args", [])
        if not tool or not isinstance(args, list):
            return {
                "op": command.get("op", "unknown"),
                "tool": tool,
                "args": args,
                "ok": False,
                "exit_code": None,
                "stderr": "invalid command spec",
            }

        cmd = [tool] + [str(arg) for arg in args]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            stdout_text = (proc.stdout or "").strip()
            stderr_text = (proc.stderr or "").strip()
            ok = proc.returncode == 0 and not stderr_text.lower().startswith("error:")
            result = {
                "op": command.get("op", "unknown"),
                "tool": tool,
                "args": [str(arg) for arg in args],
                "ok": ok,
                "exit_code": proc.returncode,
                "stdout": stdout_text[:400],
                "stderr": stderr_text[:400],
            }
            resource_handle = self._extract_resource_handle(command, stdout_text)
            if resource_handle is not None:
                result["resource_handle"] = resource_handle
            return result
        except FileNotFoundError:
            return {
                "op": command.get("op", "unknown"),
                "tool": tool,
                "args": [str(arg) for arg in args],
                "ok": False,
                "exit_code": None,
                "stderr": f"{tool} not found",
            }
        except subprocess.TimeoutExpired:
            return {
                "op": command.get("op", "unknown"),
                "tool": tool,
                "args": [str(arg) for arg in args],
                "ok": False,
                "exit_code": None,
                "stderr": "command timed out",
            }
        except Exception as exc:
            return {
                "op": command.get("op", "unknown"),
                "tool": tool,
                "args": [str(arg) for arg in args],
                "ok": False,
                "exit_code": None,
                "stderr": str(exc),
            }

    def _extract_resource_handle(self, command: Dict[str, Any], stdout: str) -> Dict[str, Any] | None:
        if str(command.get("op", "")) != "ensure_module":
            return None
        match = re.search(r"\b(\d+)\b", stdout or "")
        if not match:
            return None
        return {
            "type": "module",
            "id": int(match.group(1)),
        }

    def _teardown_previous(self, previous_state: Dict[str, Any]) -> None:
        resource_handles = previous_state.get("resource_handles", {})
        if not isinstance(resource_handles, dict):
            return
        for handle in resource_handles.values():
            if not isinstance(handle, dict):
                continue
            if handle.get("type") != "module":
                continue
            module_id = handle.get("id")
            if not isinstance(module_id, int):
                continue
            subprocess.run(
                ["pw-cli", "destroy", str(module_id)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _publish_metadata(self, mac: str, state: Dict[str, Any]) -> None:
        key = f"{METADATA_KEY_PREFIX}{mac}"
        value = json.dumps(state, separators=(",", ":"), sort_keys=True)
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

    def _write_per_output_state(self, mac: str, state: Dict[str, Any]) -> None:
        path = self._state_path(mac)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(state, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, path)

    def _remove_per_output_state(self, mac: str) -> None:
        path = self._state_path(mac)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _write_state(self, states: Dict[str, Dict[str, Any]]) -> None:
        payload = {"schema": 1, "outputs": states}
        tmp_path = f"{GRAPH_APPLY_STATE_PATH}.tmp"
        with open(tmp_path, "w", encoding="ascii") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, GRAPH_APPLY_STATE_PATH)

    def _state_path(self, mac: str) -> str:
        return os.path.join(GRAPH_APPLY_DIR, f"{mac.replace(':', '_').lower()}.json")

    def _read_executor_state(self) -> Dict[str, Any]:
        if not os.path.exists(GRAPH_EXECUTOR_STATE_PATH):
            return {"schema": 1, "outputs": {}}
        try:
            with open(GRAPH_EXECUTOR_STATE_PATH, "r", encoding="ascii") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                return {"schema": 1, "outputs": {}}
            state.setdefault("outputs", {})
            return state
        except Exception as exc:
            log.warning("Failed to read graph executor state: %s", exc)
            return {"schema": 1, "outputs": {}}

    def _digest_spec(self, spec: Dict[str, Any]) -> str:
        raw = json.dumps(spec, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(raw.encode("ascii")).hexdigest()


def main() -> None:
    applier = PipeWireGraphApplier()
    applier.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
