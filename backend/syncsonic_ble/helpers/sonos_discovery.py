"""Sonos discovery via SoCo. Returns a list of devices for Wi-Fi scan results.

Restored from the pre-foundation snapshot (``wip/full-diff-snapshot-2026-03-11``)
and reconciled with the Slice 4 architecture. Pure read-only network scan: no
audio routing, no PipeWire side effects. Safe to call from a worker thread.
"""
from __future__ import annotations

from typing import Any, Dict, List

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)


def discover_sonos(timeout: int = 5) -> List[Dict[str, Any]]:
    """Discover Sonos zone players on the local network.

    Returns a list of dicts in the WIFI_SCAN_RESULTS payload shape:
      {device_id: "sonos:<uid>", name: "<player_name>", ip: "<ip>", type: "sonos"}
    Empty list on missing soco, scan failure, or no devices.
    """
    try:
        import soco.discovery
    except ImportError:
        log.warning("[WiFiScan] soco not installed; cannot discover Sonos")
        return []

    try:
        speakers = soco.discovery.discover(timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        log.exception("[WiFiScan] Sonos discovery failed: %s", exc)
        return []

    if not speakers:
        log.info("[WiFiScan] No Sonos devices found")
        return []

    out: List[Dict[str, Any]] = []
    for speaker in speakers:
        try:
            uid = getattr(speaker, "uid", None) or ""
            name = getattr(speaker, "player_name", None) or "Sonos"
            ip = str(getattr(speaker, "ip_address", "") or "")
            if not uid:
                continue
            out.append({
                "device_id": f"sonos:{uid}",
                "name": name,
                "ip": ip,
                "type": "sonos",
            })
            log.info("[WiFiScan] Found %s (sonos:%s) at %s", name, uid, ip)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WiFiScan] Skip speaker %s: %s", speaker, exc)
    return out
