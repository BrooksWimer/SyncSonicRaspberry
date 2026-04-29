"""PipeWire node-state snapshot sampler.

Once per second, runs ``pw-dump`` and extracts a compact per-node
summary: id, type, state, format, plus the "params" we care about for
debugging audio-graph health. Emits one ``pw_node_snapshot`` event with
the full list, so the analyzer can reconstruct the audio graph at any
historical timestamp without needing to re-run pw-dump.

Why pw-dump and not pw-cli ls Node
-----------------------------------
pw-dump returns clean JSON with all properties; pw-cli ls Node returns
a human-formatted text dump that needs custom parsing. pw-dump is
slightly more expensive (full enumeration of the graph object tree)
but at 1 Hz the cost is negligible and the parser is one json.loads.

Filter out the noise
--------------------
The PipeWire graph on the Pi has ~25 nodes including v4l2 inputs, the
HDMI mailbox sink, the dummy/freewheel drivers, midi-bridge, etc. that
are not interesting for audio-stability analysis. We keep:

- virtual_out
- bluez_input.* and bluez_output.*
- syncsonic-delay-* (the per-channel pw_delay_filter nodes)
- alsa_input.usb-Jieli* (the measurement mic)

Anything else is dropped from the snapshot to keep the jsonl readable.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from syncsonic_ble.telemetry import EventType
from syncsonic_ble.telemetry.event_writer import emit
from syncsonic_ble.telemetry.samplers.base import Sampler
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

PW_DUMP_TIMEOUT_SEC = 2.0

INTERESTING_NODE_PREFIXES = (
    "virtual_out",
    "bluez_input.",
    "bluez_output.",
    "syncsonic-delay-",
    "alsa_input.usb-Jieli",
)


class PwNodeSampler(Sampler):
    name = "pw_node"
    interval_sec = 1.0

    def __init__(self) -> None:
        super().__init__()
        # The collector runs in the syncsonic user's process; pw-dump
        # needs PIPEWIRE_RUNTIME_DIR / XDG_RUNTIME_DIR to point at the
        # syncsonic instance. start_syncsonic.sh already exports this in
        # the parent environment so subprocess inherits it.
        self._env = os.environ.copy()

    def tick(self) -> None:
        nodes = self._dump_nodes()
        if nodes is None:
            return
        emit(EventType.PW_NODE_SNAPSHOT, {
            "n_nodes_total": nodes["n_nodes_total"],
            "n_nodes_kept": len(nodes["nodes"]),
            "nodes": nodes["nodes"],
        })

    def _dump_nodes(self) -> Optional[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ["pw-dump"],
                capture_output=True,
                text=True,
                timeout=PW_DUMP_TIMEOUT_SEC,
                env=self._env,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            log.debug("pw-dump failed: %s", exc)
            return None
        if result.returncode != 0:
            return None
        try:
            objs = json.loads(result.stdout)
        except (ValueError, json.JSONDecodeError) as exc:
            log.debug("pw-dump JSON parse failed: %s", exc)
            return None

        kept: List[Dict[str, Any]] = []
        n_total = 0
        for obj in objs:
            if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Node":
                continue
            n_total += 1
            info = obj.get("info") or {}
            props = info.get("props") or {}
            node_name = str(props.get("node.name", ""))
            if not any(node_name.startswith(p) for p in INTERESTING_NODE_PREFIXES):
                continue
            kept.append({
                "id": obj.get("id"),
                "name": node_name,
                "state": info.get("state"),
                "media_class": props.get("media.class"),
                "audio_format": props.get("audio.format"),
                "audio_rate": props.get("audio.rate"),
                "priority_driver": props.get("priority.driver"),
                "priority_session": props.get("priority.session"),
                "node_driver_id": props.get("node.driver-id"),
                "object_serial": props.get("object.serial"),
            })
        return {"n_nodes_total": n_total, "nodes": kept}
