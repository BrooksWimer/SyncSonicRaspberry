from __future__ import annotations

"""Common helper functions for building D-Bus object paths and property dicts.

Keeping these utilities in one file ensures that BlueZ-related objects
(`Characteristic`, `Service`, `Advertisement`, …) build their D-Bus data in a
single, consistent way.
"""

import dbus
from typing import List

# We import lazily inside the helper functions to avoid circular-import issues
# between this util module and the higher-level modules that declare the
# constants.

__all__ = [
    "dbus_path",
    "characteristic_props",
    "service_props",
    "advertisement_props",
]


def dbus_path(path: str) -> dbus.ObjectPath:  # noqa: D401 – simple helper
    """Return *path* wrapped as ``dbus.ObjectPath``.

    Centralising this in one helper prevents the three identical ``get_path``
    methods from diverging.
    """
    return dbus.ObjectPath(path)


# ---------------------------------------------------------------------------
# Property-dict builders
# ---------------------------------------------------------------------------

def characteristic_props(service_path: dbus.ObjectPath, uuid: str, flags: List[str],
                         value, notifying: bool) -> dict:
    """Return the BlueZ property dict for a GATT Characteristic."""
    from syncsonic_ble.utils.constants import GATT_CHRC_IFACE  # local import

    return {
        GATT_CHRC_IFACE: {
            "Service": service_path,
            "UUID": uuid,
            "Flags": flags,
            "Value": value,
            "Notifying": dbus.Boolean(notifying),
        }
    }


def service_props(uuid: str, primary: bool, characteristic_paths: List[dbus.ObjectPath]) -> dict:
    """Return the BlueZ property dict for a GATT Service."""
    from syncsonic_ble.utils.constants import GATT_SERVICE_IFACE  # local import

    return {
        GATT_SERVICE_IFACE: {
            "UUID": uuid,
            "Primary": dbus.Boolean(primary),
            "Characteristics": dbus.Array(characteristic_paths, signature="o"),
        }
    }


def advertisement_props(ad_type: str, service_uuids: List[str], local_name: str,
                         include_tx_power: bool, discoverable: bool) -> dict:
    """Return the BlueZ property dict for an LEAdvertisement1 object."""

    return {
        "org.bluez.LEAdvertisement1": {
            "Type": dbus.String(ad_type),
            "ServiceUUIDs": dbus.Array(service_uuids, signature="s"),
            "LocalName": dbus.String(local_name),
            "IncludeTxPower": dbus.Boolean(include_tx_power),
            "Discoverable": dbus.Boolean(discoverable),
        }
    } 