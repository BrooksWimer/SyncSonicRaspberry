from __future__ import annotations

import json
import os
import tempfile
from typing import Dict, Any

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

DSP_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
DSP_STATE_PATH = os.path.join(DSP_DIR, "dsp_state.json")


def write_dsp_state(outputs: Dict[str, Dict[str, Any]]) -> str:
    os.makedirs(DSP_DIR, exist_ok=True)
    payload = {
        "schema": 1,
        "outputs": outputs,
    }
    tmp_path = f"{DSP_STATE_PATH}.tmp"
    with open(tmp_path, "w", encoding="ascii") as fh:
        json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
    os.replace(tmp_path, DSP_STATE_PATH)
    log.info("PipeWire DSP contract updated for %d output(s)", len(outputs))
    return DSP_STATE_PATH
