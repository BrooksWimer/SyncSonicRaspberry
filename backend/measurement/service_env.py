"""Environment parsing helpers for measurement services."""

from __future__ import annotations

import os


TRUE_VALUES = {"1", "true", "yes", "on"}


def slice4_observe_from_env() -> bool:
    """Return whether Slice 4 observation is enabled by service environment."""
    return os.environ.get("SYNCSONIC_SLICE4_OBSERVE", "").strip().lower() in TRUE_VALUES
