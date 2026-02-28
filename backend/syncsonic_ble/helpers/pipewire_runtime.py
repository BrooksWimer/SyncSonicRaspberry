from __future__ import annotations

import shutil


def has_pipewire_cli() -> bool:
    return shutil.which("pw-cli") is not None


def has_pipewire_pulse() -> bool:
    return shutil.which("pipewire-pulse") is not None
