# scan_manager.py
"""scan_manager
================
Thread‑safe owner of *all* Bluetooth discovery activity.

*   **Exactly one** `StartDiscovery` / `StopDiscovery` call sequence per adapter.
*   Reference count per adapter so multiple callers can share the same scan.
*   Blocking `wait_for_device()` helper that any thread can call.
*   No sleeps: uses `threading.Condition` to wait for BlueZ `InterfacesAdded`
    signals.

The class is transport‑agnostic: Flask, BLE, CLI – anyone can call
`ensure_discovery()` + `wait_for_device()` from any thread.  All BlueZ work is
executed in the *calling* thread, but the internal state is protected by
re‑entrant locks so parallel callers behave correctly.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional
from syncsonic_ble.utils.logging_conf import get_logger
from typing import Any
import dbus

from syncsonic_ble.state_management.bus_manager import get_bus
from syncsonic_ble.helpers.adapter_helpers import adapter_proxies, device_path_on_adapter

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Helper types
# ---------------------------------------------------------------------------

class _AdapterEntry:
    __slots__ = ("proxy", "refcount")

    def __init__(self, proxy: Any):
        self.proxy: Any = proxy
        self.refcount: int = 0

# ---------------------------------------------------------------------------
# Public ScanManager
# ---------------------------------------------------------------------------

class ScanManager:
    """Serialises discovery on every adapter.

    Typical usage by connection logic::

        scan_mgr = ScanManager()           # usually one global instance
        scan_mgr.ensure_discovery(ctrl)
        path = scan_mgr.wait_for_device(ctrl, mac, 20)
        scan_mgr.release_discovery(ctrl)
    """

    # -----------------------------
    # Construction / BlueZ setup
    # -----------------------------

    def __init__(self):
        self._bus = get_bus()
        self._adapters: Dict[str, _AdapterEntry] = {}   # mac → entry
        self._lock = threading.RLock()                  # guards adapters & maps
        self._cond = threading.Condition(self._lock)    # signalled on device add

        # Subscribe once to BlueZ InterfacesAdded signals.
        self._bus.add_signal_receiver(
            self._on_interfaces_added,
            dbus_interface="org.freedesktop.DBus.ObjectManager",
            signal_name="InterfacesAdded",
        )

        # Build initial adapter map.
        self.refresh_adapters()

    # -----------------------------
    # Public discovery control
    # -----------------------------

    def ensure_discovery(self, adapter_mac: str) -> None:
        """Increment ref‑count; start discovery if it was previously idle."""
        adapter_mac = adapter_mac.upper()
        with self._lock:
            entry = self._adapters.get(adapter_mac)
            if not entry:
                raise ValueError(f"Adapter {adapter_mac} not found in BlueZ")

            if entry.refcount == 0:
                try:
                    entry.proxy.StartDiscovery()
                except Exception as e:  # noqa: BLE001
                    if "InProgress" not in str(e):
                        raise
            entry.refcount += 1

    def release_discovery(self, adapter_mac: str) -> None:
        """Decrement ref‑count; stop discovery when it reaches 0."""
        adapter_mac = adapter_mac.upper()
        with self._lock:
            entry = self._adapters.get(adapter_mac)
            if not entry:
                return
            if entry.refcount == 0:
                return  # programming error, but ignore
            entry.refcount -= 1
            if entry.refcount == 0:
                try:
                    entry.proxy.StopDiscovery()
                except Exception as e:  # noqa: BLE001
                    if "InProgress" in str(e):
                        logger.info("[ScanMgr] StopDiscovery ignored (BlueZ busy)")
                    else:
                        raise 
    def wait_for_device(
        self,
        adapter_mac: str,
        target_mac: str,
        timeout_s: int = 20,
    ) -> Optional[str]:
        """Block until *target_mac* is discovered on *adapter_mac*.

        Returns the D‑Bus object path on success; **None** on timeout.
        """
        adapter_mac = adapter_mac.upper()
        target_mac = target_mac.upper()
        deadline = time.time() + timeout_s

        with self._cond:
            # Fast‑path: maybe it is already in the object tree
            path = self._lookup_device_path(adapter_mac, target_mac)
            if path:
                return path

            # Wait until InterfacesAdded arrives or timeout.
            while time.time() < deadline:
                remaining = deadline - time.time()
                self._cond.wait(timeout=remaining)
                path = self._lookup_device_path(adapter_mac, target_mac)
                if path:
                    return path
        return None

    # -----------------------------
    # BlueZ signal handler
    # -----------------------------

    def _on_interfaces_added(self, sender, object_path, *args, **kwargs):  # noqa: D401,E501  # pragma: no cover
        # We don't care about the details; just wake up waiters.
        with self._cond:
            self._cond.notify_all()

    # -----------------------------
    # Internal helpers
    # -----------------------------

    def _lookup_device_path(self, adapter_mac: str, dev_mac: str) -> Optional[str]:
        """Return device object path if it exists under *adapter_mac*."""
        om = dbus.Interface(self._bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
        objects = om.GetManagedObjects()
        expected = device_path_on_adapter(self._bus, adapter_mac, dev_mac)
        if expected and expected in objects:
            return expected
        return None

    def refresh_adapters(self) -> None:
        """Rebuild the internal adapter map from the current BlueZ object tree.

        Call this after USB-reset scripts or whenever adapters may have been
        added/removed.  Thread-safe – it acquires the internal lock so callers
        can invoke it from any thread.
        """
        with self._lock:
            self._adapters.clear()
            for mac, proxy in adapter_proxies(self._bus).items():
                self._adapters[mac] = _AdapterEntry(proxy)