"""
Utilities for discovering, selecting, and resetting BlueZ Bluetooth adapters.

Reservation model:
- Prefer `RESERVED_ADAPTER_MAC` for stable phone-adapter binding.
- Fallback to legacy `RESERVED_HCI` for backward compatibility.
"""

from __future__ import annotations

import os
import time

import dbus
from gi.repository import GLib

from syncsonic_ble.utils.constants import (
    ADAPTER_INTERFACE,
    BLUEZ_SERVICE_NAME,
    DBUS_OM_IFACE,
    DBUS_PROP_IFACE,
    DEVICE_INTERFACE,
    LE_ADVERTISING_MANAGER_IFACE,
)
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

RESERVED_HCI = (os.getenv("RESERVED_HCI") or "").strip()
RESERVED_ADAPTER_MAC = (os.getenv("RESERVED_ADAPTER_MAC") or "").strip().upper()
if not RESERVED_HCI and not RESERVED_ADAPTER_MAC:
    raise RuntimeError("Either RESERVED_ADAPTER_MAC or RESERVED_HCI must be set")

# Lazy-loaded SystemBus instance; set by syncsonic_ble.main
_BUS = None


def set_bus(bus):
    global _BUS
    _BUS = bus


def find_adapter(preferred: str | None = None):
    """
    Find a BlueZ adapter by name or return the first available one.
    """
    om = dbus.Interface(_BUS.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
    for path, ifaces in om.GetManagedObjects().items():
        if ADAPTER_INTERFACE not in ifaces:
            continue
        if preferred and path.split("/")[-1] != preferred:
            continue
        adapter = dbus.Interface(_BUS.get_object(BLUEZ_SERVICE_NAME, path), ADAPTER_INTERFACE)
        return path, adapter
    return None, None


def list_adapters(bus):
    """
    Return adapter records from BlueZ object tree.
    Each record has keys: path, hci, address.
    """
    records = []
    om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
    for path, ifaces in om.GetManagedObjects().items():
        adapter = ifaces.get(ADAPTER_INTERFACE)
        if not adapter:
            continue
        records.append(
            {
                "path": path,
                "hci": path.split("/")[-1],
                "address": str(adapter.get("Address", "")).upper(),
            }
        )
    return records


def resolve_reserved_adapter(bus):
    """
    Resolve the reserved phone adapter with MAC-first matching, then HCI fallback.
    Returns dict(path, hci, address, source).
    """
    adapters = list_adapters(bus)

    if RESERVED_ADAPTER_MAC:
        for rec in adapters:
            if rec["address"] == RESERVED_ADAPTER_MAC:
                return {**rec, "source": "mac"}
        raise RuntimeError(
            f"Reserved adapter MAC {RESERVED_ADAPTER_MAC} not found; seen={[a['address'] for a in adapters]}"
        )

    for rec in adapters:
        if rec["hci"] == RESERVED_HCI:
            return {**rec, "source": "hci"}

    raise RuntimeError(
        f"Reserved adapter HCI {RESERVED_HCI} not found; seen={[a['hci'] for a in adapters]}"
    )


def is_reserved_adapter_path(path: str, adapter_address: str | None = None) -> bool:
    """
    Return True if this adapter path/address is reserved for phone BLE.
    """
    hci_name = path.split("/")[-1]
    address = (adapter_address or "").upper()
    if RESERVED_ADAPTER_MAC and address:
        return address == RESERVED_ADAPTER_MAC
    return bool(RESERVED_HCI) and hci_name == RESERVED_HCI


def reset_adapter(adapter):
    """
    Power-cycle the specified adapter and wait briefly.
    """
    try:
        props = dbus.Interface(_BUS.get_object(BLUEZ_SERVICE_NAME, adapter.object_path), DBUS_PROP_IFACE)
        log.debug("Power-cycling %s", adapter.object_path)
        props.Set(ADAPTER_INTERFACE, "Powered", dbus.Boolean(False))
        time.sleep(2.0)
        props.Set(ADAPTER_INTERFACE, "Powered", dbus.Boolean(True))
        GLib.idle_add(lambda: None)
        log.info("Adapter %s reset", adapter.object_path)
    except Exception as exc:
        log.error("Failed to reset adapter: %s", exc)


def get_reserved_advertising_manager(bus):
    """
    Return the advertising manager for the resolved reserved adapter.
    """
    resolved = resolve_reserved_adapter(bus)
    adapter_path = resolved["path"]
    obj = bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
    ad_mgr = dbus.Interface(obj, LE_ADVERTISING_MANAGER_IFACE)
    log.info(
        "Advertising manager acquired on %s (hci=%s mac=%s source=%s)",
        adapter_path,
        resolved["hci"],
        resolved["address"],
        resolved["source"],
    )
    return adapter_path, ad_mgr


def extract_mac(path: str) -> str | None:
    """
    Return the Bluetooth MAC (AA:BB:CC:DD:EE:FF) from a BlueZ device path.
    """
    if "dev_" not in path:
        return None
    return path.split("/")[-1].replace("dev_", "").replace("_", ":").upper()


def adapter_prefix_from_path(device_path: str) -> str:
    """
    Return the /org/bluez/hciX prefix for a given device path.
    """
    return "/".join(device_path.split("/")[:4])


def connected_devices_on_adapter(bus, adapter_prefix: str) -> list[str]:
    """
    Return MAC addresses of connected devices under adapter_prefix.
    """
    objs = _get_managed_objects(bus)

    result: list[str] = []
    for obj_path, ifaces in objs.items():
        dev = ifaces.get(DEVICE_INTERFACE)
        if not dev or not dev.get("Connected", False):
            continue
        if obj_path.startswith(adapter_prefix):
            result.append(dev["Address"])
    return result


def device_path_on_adapter(bus, ctrl_mac: str, dev_mac: str) -> str | None:
    """
    Return /org/bluez/hciX/dev_XX_YY... for dev_mac on adapter ctrl_mac.
    """
    ctrl_mac = ctrl_mac.upper()
    dev_mac_fmt = dev_mac.upper().replace(":", "_")

    objects = _get_managed_objects(bus)

    for path, ifaces in objects.items():
        adapter = ifaces.get(ADAPTER_INTERFACE)
        if not adapter:
            continue
        if adapter.get("Address", "").upper() == ctrl_mac:
            return f"{path}/dev_{dev_mac_fmt}"
    return None


def adapter_proxies(bus) -> dict[str, object]:
    """
    Return a mapping MAC -> org.bluez.Adapter1 proxy.
    """
    objects = _get_managed_objects(bus)
    proxies: dict[str, object] = {}
    for path, ifaces in objects.items():
        adapter = ifaces.get(ADAPTER_INTERFACE)
        if not adapter:
            continue
        mac = adapter.get("Address", "").upper()
        if not mac or mac in proxies:
            continue
        proxies[mac] = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, path), ADAPTER_INTERFACE)
    return proxies


def _get_managed_objects(bus):
    """
    Return the BlueZ object tree via ObjectManager.GetManagedObjects().
    """
    om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
    return om.GetManagedObjects()

