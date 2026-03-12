"""Helpers to distinguish Wi‑Fi (Sonos) vs Bluetooth device IDs."""
from __future__ import annotations


def is_sonos(device_id: str) -> bool:
    """Return True if device_id is a Sonos device (e.g. sonos:RINCON_...)."""
    return (device_id or "").lower().startswith("sonos:")


def get_device_type(device_id: str) -> str:
    """Return 'sonos' for Sonos device_id, otherwise 'bluetooth'."""
    return "sonos" if is_sonos(device_id) else "bluetooth"
