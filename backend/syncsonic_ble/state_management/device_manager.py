"""Tracks BlueZ device objects and Bluetooth connection state."""

from __future__ import annotations

import re
from typing import Dict, Set

import dbus
from gi.repository import GLib

from syncsonic_ble.helpers.adapter_helpers import (
    adapter_prefix_from_path,
    connected_devices_on_adapter,
    extract_mac,
)
from syncsonic_ble.state_management.bus_manager import get_bus
from syncsonic_ble.utils.constants import (
    A2DP_UUID,
    ADAPTER_INTERFACE,
    BLUEZ_SERVICE_NAME,
    DBUS_OM_IFACE,
    DBUS_PROP_IFACE,
    DEVICE_INTERFACE,
    Msg,
)
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)


class DeviceManager:
    """Single source of truth for Bluetooth device state on one adapter."""

    def __init__(self, bus: dbus.SystemBus, adapter_path: str):
        self.bus: dbus.SystemBus = bus
        self.adapter_path: str = adapter_path

        self.devices: Dict[str, Dict] = {}
        self.reconnect_attempts: Dict[str, int] = {}
        self.max_reconnect_attempts: int = 3
        self.pairing_in_progress: Set[str] = set()
        self.connected: Set[str] = set()
        self.expected: set[str] = set()
        self._status: Dict[str, Dict] = {}
        self._char = None
        self.scanning = False
        self._setup_monitoring()

    _extract_mac = staticmethod(extract_mac)

    def _devices_on_adapter(self, adapter_prefix: str) -> list[str]:
        return connected_devices_on_adapter(self.bus, adapter_prefix)

    def attach_characteristic(self, char) -> None:
        self._char = char

    def _setup_monitoring(self) -> None:
        self.bus.add_signal_receiver(
            self._interfaces_added,
            dbus_interface="org.freedesktop.DBus.ObjectManager",
            signal_name="InterfacesAdded",
        )
        self.bus.add_signal_receiver(
            self._properties_changed,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged",
            path_keyword="path",
        )

    def _interfaces_added(self, path, interfaces) -> None:
        if DEVICE_INTERFACE in interfaces:
            self._device_found(path)

    def _properties_changed(self, *args, **kwargs) -> None:
        path = kwargs.get("path")
        if len(args) >= 3:
            interface, changed, _invalidated = args[0], args[1], args[2]
            if len(args) >= 4:
                path = args[3]
        elif len(args) >= 2:
            interface, changed = args[0], args[1]
        else:
            return

        if interface != DEVICE_INTERFACE or "Connected" not in changed:
            return

        connected = bool(changed["Connected"])
        mac = self._extract_mac(path)
        if not mac:
            return

        log.info("[BlueZ] %s is now %s", mac, "CONNECTED" if connected else "DISCONNECTED")

        if connected:
            self._handle_new_connection(path, mac)
        else:
            self._handle_disconnection(mac)

        alias = changed.get("Alias", mac)
        self._status[mac] = {"alias": alias, "connected": connected}
        if self._char:
            self._char.push_status({"connected": list(self._all_connected())})

    def _handle_new_connection(self, path: str, mac: str) -> None:
        if mac in self.connected:
            return

        dev_obj = self.bus.get_object(BLUEZ_SERVICE_NAME, path)
        dev_props = dbus.Interface(dev_obj, DBUS_PROP_IFACE)

        try:
            uuids = dev_props.Get(DEVICE_INTERFACE, "UUIDs")
        except Exception:  # noqa: BLE001
            uuids = []
        # Do not skip when the UUID list omits A2DP-sink (0x110b): phones often
        # publish only A2DP-source (0x110a) and only expose 0x110b after the
        # BR/EDR link comes up. We still need to track the connection so the
        # LOOPBACK_SYNC reserved-adapter branch in ConnectionService can drive
        # phone-ingress; without this relaxation, the phone never reaches that
        # branch and phone audio depends solely on WirePlumber autoconnect.
        if not any("110b" in uuid.lower() for uuid in uuids):
            log.info("%s: no A2DP UUID in list yet (will still track for phone ingress)", mac)

        adapter_prefix = adapter_prefix_from_path(path)
        others = [other for other in self._devices_on_adapter(adapter_prefix) if other != mac]
        if others:
            other_path = f"{adapter_prefix}/dev_{others[0].replace(':', '_')}"
            dbus.Interface(dev_obj, DEVICE_INTERFACE).Disconnect()
            from syncsonic_ble.state_change.action_functions import remove_device_dbus

            remove_device_dbus(other_path, self.bus)
            log.warning(
                "%s tried adapter %s but %s is already there, disconnecting and removing",
                mac,
                adapter_prefix,
                others[0],
            )
            return

        self.connected.add(mac)
        log.info("Tracking %s as connected, loopback deferred to FSM", mac)
        # Nudge BlueZ to actually negotiate A2DP. Phones in particular often
        # need an explicit ConnectProfile after the BR/EDR link comes up
        # before they expose their A2DP source. Best-effort: failure here is
        # not fatal because the LOOPBACK_SYNC handler will still try to set
        # things up, and the phone may already have A2DP up via another path.
        try:
            dev_iface = dbus.Interface(dev_obj, DEVICE_INTERFACE)
            dev_iface.ConnectProfile(A2DP_UUID)
            log.info("Requested A2DP profile for %s", mac)
        except Exception as exc:  # noqa: BLE001
            log.debug("ConnectProfile(A2DP) for %s: %s", mac, exc)

        from syncsonic_ble.state_management.connection_manager import Intent, work_q

        work_q.put((Intent.LOOPBACK_SYNC, {"mac": mac, "connected": True}))

    def _handle_disconnection(self, mac: str) -> None:
        if mac not in self.connected:
            return
        self.connected.remove(mac)
        log.info("Tracking %s as disconnected, loopback removal deferred to FSM", mac)
        from syncsonic_ble.state_management.connection_manager import Intent, work_q

        work_q.put((Intent.LOOPBACK_SYNC, {"mac": mac, "connected": False}))

    def _all_connected(self) -> Set[str]:
        """Bluetooth-only connected device set for the neutral foundation."""
        return set(self.connected)

    def _device_found(self, path: str) -> None:
        mac = self._extract_mac(path)
        if not mac:
            return

        if self.scanning and self._char:
            obj = self.bus.get_object(BLUEZ_SERVICE_NAME, path)
            props = dbus.Interface(obj, DBUS_PROP_IFACE)
            name = props.Get(DEVICE_INTERFACE, "Alias") or props.Get(DEVICE_INTERFACE, "Name")
            paired = bool(props.Get(DEVICE_INTERFACE, "Paired"))
            device_info = {"mac": mac, "name": name, "paired": paired}
            log.info("[SCAN STREAM] Discovered %s (%s), paired=%s", name, mac, paired)

            if re.search(r"([0-9A-F]{2}-){2,}", name, re.IGNORECASE):
                log.info("Filtering out device: %s", name)
            else:
                self._char.send_notification(Msg.SCAN_DEVICES, {"device": device_info})
                log.info("Adding device: %s with name: %s", mac, name)
            return

        if mac.upper() not in self.connected:
            log.debug("Ignoring unexpected device %s (%s)", path, mac)
            return

        obj = self.bus.get_object(BLUEZ_SERVICE_NAME, path)
        props_iface = dbus.Interface(obj, DBUS_PROP_IFACE)
        dev_iface = dbus.Interface(obj, DEVICE_INTERFACE)
        self.devices[path] = {"device": obj, "props": props_iface, "iface": dev_iface}
        log.info("Registered expected speaker %s at %s", mac, path)
