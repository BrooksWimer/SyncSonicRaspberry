from __future__ import annotations

import json
import os
import tempfile
from typing import Dict, Any

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

CONTROL_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
CONTROL_STATE_PATH = os.path.join(CONTROL_DIR, "control_state.json")
DEFAULT_TRANSPORT_BASE_MS = 120.0
WIFI_SESSION_TRANSPORT_BASE_MS = float(os.environ.get("SYNCSONIC_WIFI_TRANSPORT_BASE_MS", "900"))


def _load_state() -> Dict[str, Any]:
    if not os.path.exists(CONTROL_STATE_PATH):
        return {"schema": 1, "globals": {}, "outputs": {}}
    try:
        with open(CONTROL_STATE_PATH, "r", encoding="ascii") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            return {"schema": 1, "globals": {}, "outputs": {}}
        state.setdefault("schema", 1)
        state.setdefault("globals", {})
        state.setdefault("outputs", {})
        return state
    except Exception as exc:
        log.warning("Failed to read PipeWire control state: %s", exc)
        return {"schema": 1, "globals": {}, "outputs": {}}


def _write_state(state: Dict[str, Any]) -> None:
    os.makedirs(CONTROL_DIR, exist_ok=True)
    tmp_path = f"{CONTROL_STATE_PATH}.tmp"
    with open(tmp_path, "w", encoding="ascii") as fh:
        json.dump(state, fh, separators=(",", ":"), sort_keys=True)
    os.replace(tmp_path, CONTROL_STATE_PATH)


def publish_output_control(
    mac: str,
    *,
    delay_ms: float,
    rate_ppm: float,
    mode: str,
    active: bool,
) -> str:
    mac = mac.upper()
    state = _load_state()
    outputs = state.setdefault("outputs", {})
    current = outputs.get(mac, {})
    if not isinstance(current, dict):
        current = {}
    current.update({
        "delay_ms": round(float(delay_ms), 3),
        "rate_ppm": round(float(rate_ppm), 3),
        "mode": str(mode),
        "active": bool(active),
    })
    outputs[mac] = current
    _write_state(state)
    log.info(
        "PipeWire control-plane publish %s -> delay=%.3f ms rate=%.3f ppm mode=%s active=%s",
        mac,
        float(delay_ms),
        float(rate_ppm),
        mode,
        active,
    )
    return CONTROL_STATE_PATH


def publish_transport_profile(*, wifi_session_active: bool) -> str:
    state = _load_state()
    globals_state = state.setdefault("globals", {})
    transport_base_ms = (
        WIFI_SESSION_TRANSPORT_BASE_MS
        if wifi_session_active
        else DEFAULT_TRANSPORT_BASE_MS
    )
    globals_state["wifi_session_active"] = bool(wifi_session_active)
    globals_state["transport_base_ms"] = round(float(transport_base_ms), 3)
    _write_state(state)
    log.info(
        "PipeWire transport profile -> wifi_session_active=%s transport_base=%.3f ms",
        bool(wifi_session_active),
        float(transport_base_ms),
    )
    return CONTROL_STATE_PATH


def publish_output_mix(
    mac: str,
    *,
    left_percent: int,
    right_percent: int,
) -> str:
    mac = mac.upper()
    state = _load_state()
    outputs = state.setdefault("outputs", {})
    current = outputs.get(mac, {})
    if not isinstance(current, dict):
        current = {}
    current.setdefault("delay_ms", 100.0)
    current.setdefault("rate_ppm", 0.0)
    current.setdefault("mode", "idle")
    current.setdefault("active", True)
    current["left_percent"] = int(max(0, min(150, left_percent)))
    current["right_percent"] = int(max(0, min(150, right_percent)))
    outputs[mac] = current
    _write_state(state)
    log.info(
        "PipeWire mix publish %s -> left=%s%% right=%s%%",
        mac,
        current["left_percent"],
        current["right_percent"],
    )
    return CONTROL_STATE_PATH


def clear_output_control(mac: str) -> str:
    mac = mac.upper()
    state = _load_state()
    outputs = state.setdefault("outputs", {})
    outputs.pop(mac, None)
    _write_state(state)
    log.info("PipeWire control-plane cleared %s", mac)
    return CONTROL_STATE_PATH


def read_control_state() -> Dict[str, Any]:
    return _load_state()


def get_transport_base_ms(default_ms: float = DEFAULT_TRANSPORT_BASE_MS) -> float:
    state = _load_state()
    globals_state = state.get("globals", {})
    if not isinstance(globals_state, dict):
        return float(default_ms)
    try:
        return max(float(default_ms), float(globals_state.get("transport_base_ms", default_ms)))
    except Exception:
        return float(default_ms)
