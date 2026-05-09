"""Thread-safe owner of Bluetooth discovery activity, plus a top-level
``scan_wifi_sonos`` helper that the BLE handler invokes on WIFI_SCAN_START."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import dbus

from syncsonic_ble.helpers.adapter_helpers import adapter_proxies, device_path_on_adapter
from syncsonic_ble.state_management.bus_manager import get_bus
from syncsonic_ble.utils.logging_conf import get_logger

logger = get_logger(__name__)


def scan_wifi_sonos(timeout: int = 5) -> List[Dict[str, Any]]:
    """Run a one-shot Sonos scan. Wrapper that defers the soco import to
    call-time so the BLE service start path does not pull soco unless the
    user actually triggers a Wi-Fi scan."""
    try:
        from syncsonic_ble.helpers.sonos_discovery import discover_sonos
    except ImportError as exc:
        logger.warning("[WiFiScan] sonos_discovery unavailable: %s", exc)
        return []
    return discover_sonos(timeout=timeout)


class _AdapterEntry:
    __slots__ = ("proxy", "refcount")

    def __init__(self, proxy: Any):
        self.proxy = proxy
        self.refcount = 0


class ScanManager:
    """Serializes Bluetooth discovery on each adapter."""

    def __init__(self):
        self._bus = get_bus()
        self._adapters: Dict[str, _AdapterEntry] = {}
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)

        self._bus.add_signal_receiver(
            self._on_interfaces_added,
            dbus_interface="org.freedesktop.DBus.ObjectManager",
            signal_name="InterfacesAdded",
        )

        self.refresh_adapters()

    def ensure_discovery(self, adapter_mac: str) -> None:
        adapter_mac = adapter_mac.upper()
        with self._lock:
            entry = self._adapters.get(adapter_mac)
            if not entry:
                raise ValueError(f"Adapter {adapter_mac} not found in BlueZ")

            if entry.refcount == 0:
                try:
                    logger.info("[ScanMgr] StartDiscovery on adapter %s", adapter_mac)
                    entry.proxy.StartDiscovery()
                except Exception as exc:  # noqa: BLE001
                    if "InProgress" not in str(exc):
                        raise
                    logger.info("[ScanMgr] StartDiscovery already in progress on adapter %s", adapter_mac)
            entry.refcount += 1

    def release_discovery(self, adapter_mac: str) -> None:
        adapter_mac = adapter_mac.upper()
        with self._lock:
            entry = self._adapters.get(adapter_mac)
            if not entry or entry.refcount == 0:
                return

            entry.refcount -= 1
            if entry.refcount == 0:
                try:
                    logger.info("[ScanMgr] StopDiscovery on adapter %s", adapter_mac)
                    entry.proxy.StopDiscovery()
                except Exception as exc:  # noqa: BLE001
                    if "InProgress" in str(exc):
                        logger.info("[ScanMgr] StopDiscovery ignored (BlueZ busy)")
                    else:
                        raise

    def wait_for_device(
        self,
        adapter_mac: str,
        target_mac: str,
        timeout_s: int = 20,
    ) -> Optional[str]:
        adapter_mac = adapter_mac.upper()
        target_mac = target_mac.upper()
        deadline = time.time() + timeout_s

        with self._cond:
            path = self._lookup_device_path(adapter_mac, target_mac)
            if path:
                logger.info("[ScanMgr] Found %s on %s before waiting: %s", target_mac, adapter_mac, path)
                return path

            # Wake on InterfacesAdded and also poll periodically: dbus-python may
            # deliver signals on the GLib mainloop while this thread waits; a missed
            # wake plus GetManagedObjects keys typed as dbus.ObjectPath (not str)
            # used to make "expected in objects" always false until timeout.
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                chunk = min(remaining, 0.25)
                self._cond.wait(timeout=chunk)
                path = self._lookup_device_path(adapter_mac, target_mac)
                if path:
                    logger.info("[ScanMgr] Found %s on %s: %s", target_mac, adapter_mac, path)
                    return path

        logger.info("[ScanMgr] Timed out waiting for %s on %s", target_mac, adapter_mac)
        return None

    def _on_interfaces_added(self, sender, object_path, *args, **kwargs):  # noqa: ANN001,D401
        with self._cond:
            self._cond.notify_all()

    def _lookup_device_path(self, adapter_mac: str, dev_mac: str) -> Optional[str]:
        om = dbus.Interface(
            self._bus.get_object("org.bluez", "/"),
            "org.freedesktop.DBus.ObjectManager",
        )
        objects = om.GetManagedObjects()
        expected = device_path_on_adapter(self._bus, adapter_mac, dev_mac)
        if not expected:
            return None
        # GetManagedObjects keys are dbus.ObjectPath; plain str membership often fails.
        expected_s = str(expected)
        for key in objects:
            if str(key) == expected_s:
                return expected_s
        return None

    def refresh_adapters(self) -> None:
        with self._lock:
            self._adapters.clear()
            for mac, proxy in adapter_proxies(self._bus).items():
                self._adapters[mac] = _AdapterEntry(proxy)
