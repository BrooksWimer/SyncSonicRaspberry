"""
Utilities for discovering, selecting, and resetting BlueZ Bluetooth adapters.

This helper module centralizes common adapter operations used throughout the
SyncSonic BLE codebase, such as:

- Finding an adapter by name (e.g. ``hci0``) or automatically picking the
  first available controller.
- Gracefully power-cycling an adapter to recover from transient errors.
- Working with advertising managers and device/controller object paths
  exposed by BlueZ via D-Bus.
- Translating between BlueZ D-Bus object paths and canonical MAC address
  strings (``AA:BB:CC:DD:EE:FF``).

All helper functions assume that :func:`set_bus` has been invoked at program
start-up to provide a lazily-initialised :class:`dbus.SystemBus` instance.

Environment variables
---------------------
RESERVED_HCI
    Name of the controller (e.g. ``hci1``) that must remain reserved for phone
    advertisement.  The application raises :class:`RuntimeError` at import-time
    if the variable is missing because we always need to know which controller
    is used for advertising.
"""
from __future__ import annotations
import dbus, os, time
from gi.repository import GLib
from syncsonic_ble.utils.constants import (
    BLUEZ_SERVICE_NAME,
    ADAPTER_INTERFACE,
    DBUS_OM_IFACE,
    DBUS_PROP_IFACE,
    LE_ADVERTISING_MANAGER_IFACE,
    DEVICE_INTERFACE,
)
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

# Check if the RESERVED_HCI environment variable is set, which is required to identify the phone adapter.
RESERVED_HCI = os.getenv("RESERVED_HCI")
if not RESERVED_HCI:
    raise RuntimeError("RESERVED_HCI environment variable not set – cannot pick phone adapter")

# Lazy-loaded SystemBus instance; set by syncsonic_ble.main
_BUS = None

def set_bus(bus):
    global _BUS
    _BUS = bus

def find_adapter(preferred: str | None = None):
    """
    Find a BlueZ adapter by name or return the first available one.
    Args:
        preferred: Optional name of the preferred adapter (e.g., 'hci0').
    Returns:
        Tuple of (adapter_path, adapter_interface) or (None, None) if no adapter is found.
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

def reset_adapter(adapter):
    """
    Power-cycles the specified adapter and waits a little.
    No device cleanup is performed here (ConnectionService handles that).
    Args:
        adapter: The adapter interface to reset.
    """
    try:
        props = dbus.Interface(_BUS.get_object(BLUEZ_SERVICE_NAME, adapter.object_path), DBUS_PROP_IFACE)
        log.debug("Power-cycling %s", adapter.object_path)
        props.Set(ADAPTER_INTERFACE, "Powered", dbus.Boolean(False))
        time.sleep(2.0)
        props.Set(ADAPTER_INTERFACE, "Powered", dbus.Boolean(True))
        GLib.idle_add(lambda: None)  # let mainloop breathe
        log.info("Adapter %s reset", adapter.object_path)
    except Exception as exc:
        log.error("Failed to reset adapter: %s", exc)

def get_reserved_advertising_manager(bus):
    """
    Return the advertising manager for the adapter specified by the RESERVED_HCI environment variable.
    Raises RuntimeError if the environment variable is missing or the adapter cannot be accessed.
    Args:
        bus: The D-Bus system bus instance.
    Returns:
        Tuple of (adapter_path, LEAdvertisingManager1).
    """
    hci = os.getenv("RESERVED_HCI")
    if not hci:
        raise RuntimeError("RESERVED_HCI not set – cannot pick phone adapter")

    adapter_path = f"/org/bluez/{hci}"
    obj = bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
    ad_mgr = dbus.Interface(obj, LE_ADVERTISING_MANAGER_IFACE)
    log.info("Advertising manager acquired on %s", adapter_path)
    return adapter_path, ad_mgr

def extract_mac(path: str) -> str | None:
    """
    Return the Bluetooth MAC (AA:BB:CC:DD:EE:FF) from a BlueZ device path.
    Example: /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF → AA:BB:CC:DD:EE:FF
    Args:
        path: The BlueZ device path.
    Returns:
        The MAC address as a string, or None if not found.
    """
    if "dev_" not in path:
        return None
    return path.split("/")[-1].replace("dev_", "").replace("_", ":").upper()

def adapter_prefix_from_path(device_path: str) -> str:
    """
    Return the /org/bluez/hciX prefix for a given device path.
    Args:
        device_path: The full device path.
    Returns:
        The adapter prefix as a string.
    """
    return "/".join(device_path.split("/")[:4])

def connected_devices_on_adapter(bus, adapter_prefix: str) -> list[str]:
    """
    Return MAC addresses of *Connected* devices under *adapter_prefix*.
    Args:
        bus: The D-Bus system bus instance.
        adapter_prefix: The prefix of the adapter path.
    Returns:
        A list of MAC addresses of connected devices.
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
    Return /org/bluez/hciX/dev_XX_YY_… for *dev_mac* on adapter *ctrl_mac*.
    Args:
        bus: The D-Bus system bus instance.
        ctrl_mac: The MAC address of the controller.
        dev_mac: The MAC address of the device.
    Returns:
        The device path as a string, or None if not found.
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
    Return a mapping **MAC → org.bluez.Adapter1 proxy** for *bus* (works with both libraries).
    Args:
        bus: The D-Bus system bus instance.
    Returns:
        A dictionary mapping MAC addresses to adapter proxies.
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
    Args:
        bus: The D-Bus system bus instance.
    Returns:
        The managed objects as a dictionary.
    """
    om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
    return om.GetManagedObjects()