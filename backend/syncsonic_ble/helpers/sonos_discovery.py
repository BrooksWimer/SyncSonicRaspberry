"""Sonos discovery via SoCo. Returns list of devices for Wi‑Fi scan results."""
from __future__ import annotations

from typing import List, Dict, Any

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)


def discover_sonos(timeout: int = 5) -> List[Dict[str, Any]]:
    """
    Discover Sonos speakers on the network.

    Returns a list of dicts suitable for scan results:
        device_id: "sonos:<uid>"
        name: player name (e.g. "Living Room")
        ip: IP address string (do not use as identity)
        type: "sonos"
    """
    try:
        import soco.discovery
    except ImportError:
        log.warning("[WiFiScan] soco not installed – cannot discover Sonos")
        return []

    try:
        speakers = soco.discovery.discover(timeout=timeout)
    except Exception as e:
        log.exception("[WiFiScan] Sonos discovery failed: %s", e)
        return []

    if not speakers:
        log.info("[WiFiScan] No Sonos devices found")
        return []

    result = []
    for speaker in speakers:
        try:
            uid = getattr(speaker, "uid", None) or ""
            name = getattr(speaker, "player_name", None) or "Sonos"
            ip = str(getattr(speaker, "ip_address", "") or "")
            if uid:
                device_id = f"sonos:{uid}"
                result.append({
                    "device_id": device_id,
                    "name": name,
                    "ip": ip,
                    "type": "sonos",
                })
                log.info("[WiFiScan] Found %s (%s) at %s", name, device_id, ip)
        except Exception as e:
            log.warning("[WiFiScan] Skip speaker %s: %s", speaker, e)

    return result
