from __future__ import annotations

import json
import os
import tempfile
from typing import Dict, Any

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

DSP_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
DSP_STATE_PATH = os.path.join(DSP_DIR, "dsp_state.json")
_LAST_DSP_STATE_SERIALIZED: str = ""


def write_dsp_state(outputs: Dict[str, Dict[str, Any]]) -> str:
    global _LAST_DSP_STATE_SERIALIZED
    os.makedirs(DSP_DIR, exist_ok=True)
    payload = {
        "schema": 1,
        "outputs": outputs,
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if serialized == _LAST_DSP_STATE_SERIALIZED:
        return DSP_STATE_PATH
    tmp_path = f"{DSP_STATE_PATH}.tmp"
    with open(tmp_path, "w", encoding="ascii") as fh:
        fh.write(serialized)
    os.replace(tmp_path, DSP_STATE_PATH)
    _LAST_DSP_STATE_SERIALIZED = serialized
    log.debug("PipeWire DSP contract updated for %d output(s)", len(outputs))
    return DSP_STATE_PATH


def read_dsp_state() -> Dict[str, Any]:
    if not os.path.exists(DSP_STATE_PATH):
        return {"schema": 1, "outputs": {}}
    try:
        with open(DSP_STATE_PATH, "r", encoding="ascii") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            return {"schema": 1, "outputs": {}}
        state.setdefault("schema", 1)
        state.setdefault("outputs", {})
        return state
    except Exception as exc:
        log.warning("Failed to read PipeWire DSP state: %s", exc)
        return {"schema": 1, "outputs": {}}
