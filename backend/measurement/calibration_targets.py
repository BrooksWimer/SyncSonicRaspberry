"""Persistent startup-tune target_total_ms state.

Runtime ultrasonic correction needs to use the wall-clock target that was last
used for startup-tune calibration. BLE handlers write that value here; the
runtime service reads it per speaker, falling back to a shared value and then
to its CLI default only when no persistent file exists.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


TARGETS_PATH = Path(
    os.environ.get("SYNCSONIC_CALIBRATION_TARGETS_PATH", "/var/lib/syncsonic/startup_tune_targets.json")
)


@dataclass(frozen=True)
class TargetTotalResolution:
    target_total_ms: float
    source: str
    path: Path


def record_startup_tune_target(
    target_total_ms: float,
    *,
    mac: Optional[str] = None,
    path: Path = TARGETS_PATH,
) -> Path:
    """Persist a startup-tune target.

    ``mac=None`` records the shared target used by Align All. A MAC records a
    per-speaker target, which takes precedence when runtime correction runs.
    """
    target = _coerce_target(target_total_ms)
    existing = _load_existing(path)
    now = time.time()
    state: dict[str, Any] = {
        "schema": 1,
        "updated_unix": now,
        "shared": existing.get("shared") if isinstance(existing.get("shared"), dict) else {},
        "speakers": existing.get("speakers") if isinstance(existing.get("speakers"), dict) else {},
    }
    if mac:
        speaker_mac = mac.upper()
        speakers = state.setdefault("speakers", {})
        speakers[speaker_mac] = {
            "target_total_ms": target,
            "updated_unix": now,
        }
    else:
        state["shared"] = {
            "target_total_ms": target,
            "updated_unix": now,
        }
    _atomic_write_json(path, state)
    return path


def read_startup_tune_target(
    mac: str,
    cli_default_ms: float,
    *,
    path: Path = TARGETS_PATH,
) -> TargetTotalResolution:
    """Resolve target_total_ms for runtime correction.

    Precedence is per-speaker persistent value, shared persistent value, then
    CLI default when the persistent file is absent or unusable.
    """
    fallback = _coerce_target(cli_default_ms)
    if not path.exists():
        return TargetTotalResolution(fallback, "cli_default_missing_file", path)
    try:
        with path.open("r", encoding="ascii") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return TargetTotalResolution(fallback, "cli_default_unreadable_file", path)
    if not isinstance(state, dict):
        return TargetTotalResolution(fallback, "cli_default_invalid_file", path)

    speakers = state.get("speakers")
    if isinstance(speakers, dict):
        speaker_state = speakers.get(mac.upper())
        if isinstance(speaker_state, dict):
            value = _finite_target_or_none(speaker_state.get("target_total_ms"))
            if value is not None:
                return TargetTotalResolution(value, "per_speaker", path)

    shared = state.get("shared")
    if isinstance(shared, dict):
        value = _finite_target_or_none(shared.get("target_total_ms"))
        if value is not None:
            return TargetTotalResolution(value, "shared", path)

    return TargetTotalResolution(fallback, "cli_default_no_persistent_target", path)


def _load_existing(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="ascii") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _atomic_write_json(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="ascii") as fh:
        json.dump(state, fh, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, path)


def _finite_target_or_none(value: Any) -> Optional[float]:
    try:
        target = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(target):
        return None
    return target


def _coerce_target(value: float) -> float:
    target = float(value)
    if not math.isfinite(target):
        raise ValueError(f"target_total_ms must be finite, got {value!r}")
    return target
