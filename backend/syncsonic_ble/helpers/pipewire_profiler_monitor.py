from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Dict

from syncsonic_ble.helpers.device_labels import format_device_label
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

PROFILER_SAMPLE_SEC = float(os.environ.get("SYNCSONIC_PROF_SAMPLE_SEC", "2.0"))
DROP_LOG_COOLDOWN_SEC = float(os.environ.get("SYNCSONIC_DROP_LOG_COOLDOWN_SEC", "8.0"))
PROFILER_STATE_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
PROFILER_STATE_PATH = os.path.join(PROFILER_STATE_DIR, "profiler_state.json")

FILTER_RE = re.compile(r"(syncsonic-delay-([0-9a-f_]+)-(fl|fr))", re.IGNORECASE)
SINK_RE = re.compile(r"(bluez_output\.([0-9a-f_]+)\.[^\s]+)", re.IGNORECASE)
STATUS_RE = re.compile(r"^\s*([A-Za-z])\s+")
ERR_RE = re.compile(r"\s(\d+)\s+[A-Z][0-9A-Z]*LE\b")


class PipeWireProfilerMonitor:
    """Collects lightweight runtime health from pw-top batch snapshots."""

    def __init__(self) -> None:
        self._last_sample_ts: float = 0.0
        self._state: Dict[str, Dict[str, Any]] = {}
        self._last_node_err: Dict[str, int] = {}
        self._last_node_status: Dict[str, str] = {}
        self._last_drop_log_ts: Dict[str, float] = {}

    def sample(self) -> Dict[str, Dict[str, Any]]:
        now = time.monotonic()
        if (now - self._last_sample_ts) < PROFILER_SAMPLE_SEC and self._state:
            return self._state
        self._last_sample_ts = now

        lines = self._run_pw_top()
        snapshot: Dict[str, Dict[str, Any]] = {}
        for line in lines:
            node_info = self._extract_node_info(line)
            if node_info is None:
                continue
            node_name = node_info["node_name"]
            mac = node_info["mac"]
            channel = node_info["channel"]
            node_kind = node_info["kind"]
            status_match = STATUS_RE.search(line)
            status = status_match.group(1).upper() if status_match else "?"
            err = self._extract_err(line)

            node_key = f"{mac}:{channel}"
            prev_err = self._last_node_err.get(node_key, err)
            prev_status = self._last_node_status.get(node_key, status)
            if err > prev_err:
                if node_kind == "sink":
                    self._log_drop(
                        key=f"{mac}:{channel}:err",
                        message=(
                            "[DROP] speaker=%s source=bluez_output channel=%s reason=err_increment "
                            "err=%d->%d node=%s"
                        ),
                        args=(format_device_label(mac), channel, prev_err, err, node_name),
                        level="warning",
                    )
                else:
                    log.debug(
                        "[PROF] speaker=%s channel=%s xrun_err=%d->%d node=%s",
                        format_device_label(mac),
                        channel,
                        prev_err,
                        err,
                        node_name,
                    )
            if status != prev_status:
                if node_kind == "sink":
                    if status != "R":
                        self._log_drop(
                            key=f"{mac}:{channel}:status_bad",
                            message=(
                                "[DROP] speaker=%s source=bluez_output channel=%s reason=status_change "
                                "status=%s->%s node=%s"
                            ),
                            args=(format_device_label(mac), channel, prev_status, status, node_name),
                            level="warning",
                        )
                    elif prev_status != "R":
                        log.info(
                            "[DROP] speaker=%s source=bluez_output channel=%s reason=recovered "
                            "status=%s->%s node=%s",
                            format_device_label(mac),
                            channel,
                            prev_status,
                            status,
                            node_name,
                        )
                else:
                    log.debug(
                        "[PROF] speaker=%s channel=%s status=%s->%s node=%s",
                        format_device_label(mac),
                        channel,
                        prev_status,
                        status,
                        node_name,
                    )
            self._last_node_err[node_key] = err
            self._last_node_status[node_key] = status

            entry = snapshot.setdefault(
                mac,
                {
                    "channels": {},
                    "err_total": 0,
                    "updated_at_unix": round(time.time(), 3),
                },
            )
            channel_entry = {
                "node": node_name,
                "kind": node_kind,
                "status": status,
                "err": int(err),
            }
            entry["channels"][channel] = channel_entry
            entry["err_total"] = int(entry.get("err_total", 0)) + int(err)

        self._state = snapshot
        self._persist_state()
        return self._state

    def _extract_node_info(self, line: str) -> Dict[str, str] | None:
        filter_match = FILTER_RE.search(line)
        if filter_match:
            return {
                "node_name": filter_match.group(1),
                "mac": filter_match.group(2).upper().replace("_", ":"),
                "channel": filter_match.group(3).lower(),
                "kind": "filter",
            }
        sink_match = SINK_RE.search(line)
        if sink_match:
            return {
                "node_name": sink_match.group(1),
                "mac": sink_match.group(2).upper().replace("_", ":"),
                "channel": "sink",
                "kind": "sink",
            }
        return None

    def _log_drop(self, *, key: str, message: str, args: tuple[Any, ...], level: str) -> None:
        now = time.time()
        last = self._last_drop_log_ts.get(key, 0.0)
        if (now - last) < DROP_LOG_COOLDOWN_SEC:
            return
        self._last_drop_log_ts[key] = now
        if level == "warning":
            log.warning(message, *args)
        else:
            log.info(message, *args)

    def get_output_snapshot(self, mac: str) -> Dict[str, Any]:
        mac = (mac or "").upper()
        return dict(self._state.get(mac, {}))

    def _run_pw_top(self) -> list[str]:
        try:
            result = subprocess.run(
                ["pw-top", "-b", "-n", "1"],
                capture_output=True,
                text=True,
                timeout=2.5,
            )
        except FileNotFoundError:
            return []
        except Exception:
            return []
        if result.returncode != 0:
            return []
        return result.stdout.splitlines()

    def _extract_err(self, line: str) -> int:
        matches = list(ERR_RE.finditer(line))
        if not matches:
            return 0
        try:
            return int(matches[-1].group(1))
        except Exception:
            return 0

    def _persist_state(self) -> None:
        try:
            os.makedirs(PROFILER_STATE_DIR, exist_ok=True)
            payload = {
                "schema": 1,
                "updated_at_unix": round(time.time(), 3),
                "outputs": self._state,
            }
            tmp_path = f"{PROFILER_STATE_PATH}.tmp"
            with open(tmp_path, "w", encoding="ascii") as fh:
                json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
            os.replace(tmp_path, PROFILER_STATE_PATH)
        except Exception:
            pass
