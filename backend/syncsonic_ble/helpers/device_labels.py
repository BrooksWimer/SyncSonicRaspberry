from __future__ import annotations

import json
import os
import tempfile
from threading import Lock
from typing import Dict, Optional

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

LABEL_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_pipewire")
LABEL_PATH = os.path.join(LABEL_DIR, "device_labels.json")

_LOCK = Lock()
_LABELS: Dict[str, str] = {}
_LOADED = False
_LAST_MTIME: float = 0.0


def _load_labels_if_needed() -> None:
    global _LOADED
    global _LAST_MTIME
    if os.path.exists(LABEL_PATH):
        try:
            mtime = float(os.path.getmtime(LABEL_PATH))
        except Exception:
            mtime = 0.0
    else:
        mtime = 0.0

    if _LOADED and mtime == _LAST_MTIME:
        return
    _LOADED = True
    if not os.path.exists(LABEL_PATH):
        _LABELS.clear()
        _LAST_MTIME = 0.0
        return
    try:
        with open(LABEL_PATH, "r", encoding="ascii") as fh:
            state = json.load(fh)
        _LABELS.clear()
        if isinstance(state, dict):
            for key, value in state.items():
                if isinstance(key, str) and isinstance(value, str):
                    _LABELS[key.upper()] = value.strip()
        _LAST_MTIME = mtime
    except Exception as exc:
        log.debug("Failed to load device labels: %s", exc)


def _persist_labels() -> None:
    os.makedirs(LABEL_DIR, exist_ok=True)
    tmp_path = f"{LABEL_PATH}.tmp"
    with open(tmp_path, "w", encoding="ascii") as fh:
        json.dump(_LABELS, fh, separators=(",", ":"), sort_keys=True)
    os.replace(tmp_path, LABEL_PATH)
    try:
        global _LAST_MTIME
        _LAST_MTIME = float(os.path.getmtime(LABEL_PATH))
    except Exception:
        pass


def register_device_label(mac: str, label: str) -> None:
    if not mac or not label:
        return
    normalized_mac = mac.upper().strip()
    normalized_label = label.strip()
    if not normalized_label:
        return
    with _LOCK:
        _load_labels_if_needed()
        previous = _LABELS.get(normalized_mac, "")
        if previous == normalized_label:
            return
        _LABELS[normalized_mac] = normalized_label
        _persist_labels()


def get_device_label(mac: str) -> str:
    if not mac:
        return ""
    with _LOCK:
        _load_labels_if_needed()
        return _LABELS.get(mac.upper().strip(), "")


def format_device_label(mac: str) -> str:
    normalized_mac = (mac or "").upper().strip()
    if not normalized_mac:
        return ""
    label = get_device_label(normalized_mac)
    if not label:
        return normalized_mac
    return f"{label} ({normalized_mac})"
