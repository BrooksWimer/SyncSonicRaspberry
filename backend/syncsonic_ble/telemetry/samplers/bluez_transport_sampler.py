"""BlueZ MediaTransport1 properties sampler (every 5s).

For each currently-active A2DP MediaTransport in BlueZ, snapshot the
slow-changing properties (Codec, Volume, Delay, Configuration). These
properties change rarely; a 5-second cadence is enough to catch
mid-stream codec renegotiation or BlueZ changing the SBC bitpool.

Why this matters for the analyzer
---------------------------------
The 2026-04-29 field experiment in proposal Section 9 surfaced that the
VIZIO and the third speaker negotiate SBC bitpool 53 while the JBL
negotiates 40. That bitpool difference is a load-bearing input into any
later "this speaker is more vulnerable to RF interference" analysis,
and we currently have no automatic record of it. This sampler captures
the SBC Configuration bytes once every 5s so the analyzer can decode
sample-rate / channel-mode / block-length / subbands / allocation /
min-bitpool / max-bitpool per device per session.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import dbus

from syncsonic_ble.telemetry import EventType
from syncsonic_ble.telemetry.event_writer import emit
from syncsonic_ble.telemetry.samplers.base import Sampler
from syncsonic_ble.utils.constants import (
    BLUEZ_SERVICE_NAME,
    DBUS_OM_IFACE,
    DEVICE_INTERFACE,
)
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

MEDIA_TRANSPORT_IFACE = "org.bluez.MediaTransport1"


class BluezTransportSampler(Sampler):
    name = "bluez_transport"
    interval_sec = 5.0

    def __init__(self, bus: Optional[dbus.SystemBus] = None) -> None:
        super().__init__()
        self._bus = bus or dbus.SystemBus()

    def tick(self) -> None:
        try:
            snapshots = self._collect_snapshots()
        except Exception as exc:  # noqa: BLE001
            log.debug("BluezTransportSampler tick failed: %s", exc)
            return
        if not snapshots:
            return
        emit(EventType.BLUEZ_TRANSPORT_SNAPSHOT, {
            "n_transports": len(snapshots),
            "transports": snapshots,
        })

    def _collect_snapshots(self) -> List[Dict[str, Any]]:
        om = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
        objects = om.GetManagedObjects()

        # Build path -> mac map by walking devices first.
        mac_for_dev: Dict[str, str] = {}
        for path, ifaces in objects.items():
            dev = ifaces.get(DEVICE_INTERFACE)
            if dev:
                mac_for_dev[path] = str(dev.get("Address", "")).upper()

        snapshots: List[Dict[str, Any]] = []
        for path, ifaces in objects.items():
            transport = ifaces.get(MEDIA_TRANSPORT_IFACE)
            if not transport:
                continue
            # MediaTransport paths look like /org/bluez/hciX/dev_<mac>/sepN/fdM
            # Extract the device path (drop /sepN/fdM) to look up the mac.
            parts = path.split("/")
            if len(parts) < 5:
                continue
            dev_path = "/".join(parts[:5])
            mac = mac_for_dev.get(dev_path, "")
            cfg = transport.get("Configuration")
            cfg_bytes = list(cfg) if cfg is not None else []
            snapshots.append({
                "path": path,
                "mac": mac,
                "state": str(transport.get("State", "")),
                "codec": int(transport.get("Codec", -1)),
                "volume": int(transport.get("Volume", 0)) if "Volume" in transport else None,
                "delay": int(transport.get("Delay", 0)) if "Delay" in transport else None,
                "configuration_bytes": cfg_bytes,
            })
        return snapshots
