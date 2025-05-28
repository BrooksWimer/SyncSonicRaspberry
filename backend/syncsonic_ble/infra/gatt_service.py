from __future__ import annotations

"""Central module holding all GATT-related DBus services & helpers.

This file aggregates what used to live in several separate modules:

• GattService / Application   – org.bluez.GattService1 wrapper
• Characteristic              – org.bluez.GattCharacteristic1 implementation
• ClientConfigDescriptor      – org.bluez.GattDescriptor1 (CCCD)
• Advertisement               – org.bluez.LEAdvertisement1 helper

Collocating the classes in one place makes cross-references easier and
simplifies external imports:

    from syncsonic_ble.core.gatt_service import (
        GattService, Application, Characteristic,
        ClientConfigDescriptor, Advertisement,
    )
"""

# stdlib ------------------------------------------------------------------
import json
from typing import Dict, Any

# Third-party --------------------------------------------------------------
import dbus
import dbus.service

# First-party --------------------------------------------------------------
from syncsonic_ble.utils.logging_conf import get_logger
from syncsonic_ble.utils.constants import (
    # General DBus / GATT consts
    DBUS_OM_IFACE, DBUS_PROP_IFACE,
    GATT_SERVICE_IFACE, GATT_CHRC_IFACE,
    # Characteristic helpers
    Msg,
    # Descriptor helpers
    CCCD_UUID,
    # Advertising helpers
    SERVICE_UUID, LE_ADVERTISING_MANAGER_IFACE,
)
from syncsonic_ble.helpers.dbus_helpers import (
    service_props, characteristic_props, advertisement_props, dbus_path,
)
from syncsonic_ble.state_change.action_request_handlers import (
    HANDLERS as _HANDLERS,
    unknown_handler as _UNKNOWN_HANDLER,
)

log = get_logger(__name__)

class DBusPathMixin:
    """Provide a single, reusable ``get_path`` implementation.

    Any D-Bus object that exposes a ``self.path`` string can inherit from this
    mixin instead of re-implementing an identical ``get_path`` method.
    """

    def get_path(self) -> dbus.ObjectPath:  # noqa: D401 – simple helper
        """Return ``self.path`` wrapped as ``dbus.ObjectPath``."""
        return dbus_path(self.path) 

# ──────────────────────────────────────────────────────────────────────────
#  GATT SERVICE & APPLICATION
# ──────────────────────────────────────────────────────────────────────────

class GattService(dbus.service.Object, DBusPathMixin):
    """A single GATT service, exposing its characteristics."""

    def __init__(self, bus, index: int, uuid: str, primary: bool = True):
        # D-Bus object path, e.g. "/org/bluez/example/service0"
        self.path            = f"/org/bluez/example/service{index}"
        self.bus             = bus
        self.uuid            = uuid
        self.primary         = primary
        self.characteristics = []
        super().__init__(bus, self.path)
        log.info("GattService created at %s (UUID=%s)", self.path, uuid)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def add_characteristic(self, ch: dbus.service.Object) -> None:
        self.characteristics.append(ch)

    # ------------------------------------------------------------------
    # D-Bus Properties helpers
    # ------------------------------------------------------------------

    def get_properties(self) -> dict:
        """Return the org.bluez.GattService1 property dict."""
        char_paths = [c.get_path() for c in self.characteristics]
        return service_props(self.uuid, self.primary, char_paths)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface: str, prop: str):  # noqa: N802 – DBus naming
        return self.get_properties()[interface][prop]

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str):  # noqa: N802 – DBus naming
        return self.get_properties()[interface]


class Application(dbus.service.Object, DBusPathMixin):
    """Root container that BlueZ introspects via ObjectManager."""

    PATH = "/com/syncsonic/app"

    def __init__(self, bus):
        self.path = self.PATH
        self.services: list[GattService] = []
        super().__init__(bus, self.path)

    # Public API -----------------------------------------------------------

    def add_service(self, service: GattService):
        self.services.append(service)

    # ObjectManager implementation ----------------------------------------

    @dbus.service.method(DBUS_OM_IFACE, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):  # noqa: N802 – DBus naming
        reply: dict[str, dict] = {}

        for svc in self.services:
            # 1) service node
            reply[svc.get_path()] = svc.get_properties()

            # 2) characteristic nodes
            for chrc in svc.characteristics:
                reply[chrc.get_path()] = chrc.get_properties()

                # 3) descriptor nodes under each characteristic
                for desc in getattr(chrc, "descriptors", []):
                    reply[desc.get_path()] = desc.get_properties()

        return reply


# ──────────────────────────────────────────────────────────────────────────
#  CHARACTERISTIC IMPLEMENTATION (JSON protocol)
# ──────────────────────────────────────────────────────────────────────────

class Characteristic(dbus.service.Object, DBusPathMixin):
    """GATT characteristic exposing the SyncSonic JSON command protocol."""

    def __init__(self, bus, index: int, uuid: str, flags, service: GattService):
        # object path sits under the service
        self.path  = f"{service.get_path()}/char{index}"
        self.bus   = bus
        self.uuid  = uuid
        self.flags = flags
        self.service = service

        self.value: list[dbus.Byte] = [dbus.Byte(0)] * 5
        self.notifying = False

        self.connected_devices: set[str] = set()
        self.device_manager = None  # injected later
        self.connection_service = None  # injected later

        self._scan_mgr = None
        self._scan_adapter_mac = None

        super().__init__(bus, self.path)
        log.info("Characteristic created (%s)", uuid)

    # ------------------------------------------------------------------
    # Dependency injection helpers
    # ------------------------------------------------------------------

    def set_device_manager(self, device_manager):
        """Give me a handle to DeviceManager so I can call back."""
        self.device_manager = device_manager

    def set_connection_service(self, service):
        """Inject the ConnectionService instance so I can enqueue intents."""
        self.connection_service = service

    # ------------------------------------------------------------------
    # Notification helpers
    # ------------------------------------------------------------------

    def send_notification(self, msg_type: Msg, payload: Dict[str, Any]):
        """Encode *payload* and emit a BLE notification if enabled."""
        # 1) Encode → bytes
        data = self._encode(msg_type, payload)

        # 2) Log for full visibility
        log.info(
            "→ [BLE Notify] type=%s(0x%02x) payload=%s",
            msg_type.name,
            msg_type.value,
            payload,
        )

        # 3) Fire if client subscribes
        if self.notifying:
            self.PropertiesChanged(
                GATT_CHRC_IFACE,
                {"Value": dbus.Array(data, signature="y")},
                [],
            )

    # Thin wrapper for legacy callers
    def push_status(self, payload: Dict[str, Any]):
        self.send_notification(Msg.SUCCESS, payload)

    # ------------------------------------------------------------------
    # DBus helpers
    # ------------------------------------------------------------------

    def get_properties(self):
        """Return the org.bluez.GattCharacteristic1 prop dict."""
        return characteristic_props(
            service_path=self.service.get_path(),
            uuid=self.uuid,
            flags=self.flags,
            value=self.value,
            notifying=self.notifying,
        )

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):  # noqa: N802 – DBus naming
        if prop == "Value":
            return dbus.Array(self.value, signature="y")
        return None

    # ------------------------------------------------------------------
    # Read / Write – JSON protocol
    # ------------------------------------------------------------------

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="aya{sv}")
    def WriteValue(self, value, options):  # noqa: N802 – DBus naming
        log.info("💥 Backend WriteValue fired! raw value=%s options=%s", value, options)

        # CCCD write? (enable/disable notifications)
        if len(value) == 2 and value[0] == 0x01:
            self.notifying = (value[1] == 0x01)
            log.info(
                "Notifications %s via CCCD",
                "enabled" if self.notifying else "disabled",
            )
            return

        # Normal command --------------------------------------------------
        msg_type, data = self._decode(value)
        handler = _HANDLERS.get(msg_type, _UNKNOWN_HANDLER)

        response = handler(self, data)
        self.value = response
        if self.notifying:
            self.PropertiesChanged(
                GATT_CHRC_IFACE,
                {"Value": dbus.Array(self.value)},
                [],
            )

    # ------------------------------------------------------------------
    # Protocol encode/decode helpers
    # ------------------------------------------------------------------

    def _encode(self, msg: Msg, payload: Dict[str, Any]):
        raw = json.dumps(payload).encode()
        out = [dbus.Byte(msg)] + [dbus.Byte(b) for b in raw]
        return out

    def _decode(self, value):
        try:
            msg = Msg(value[0])
            if len(value) == 1:
                return msg, {}
            data = json.loads(bytes(value[1:]).decode())
            log.info("🧩 Decoded msg_type=%s, data=%s", msg, data)
            return msg, data
        except Exception as exc:
            log.error("decode error: %s", exc)
            return Msg.ERROR, {"error": str(exc)}

    # ------------------------------------------------------------------
    # Notify start/stop ---------------------------------------------------

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):  # noqa: N802 – DBus naming
        if self.notifying:
            log.info("Already notifying, ignoring StartNotify")
            return
        self.notifying = True
        log.info("Notifications enabled via StartNotify")

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):  # noqa: N802 – DBus naming
        if not self.notifying:
            log.info("Not notifying, ignoring StopNotify")
            return
        self.notifying = False
        log.info("Notifications disabled via StopNotify")

    # DBus signal ---------------------------------------------------------

    @dbus.service.signal(DBUS_PROP_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed_properties, invalidated_properties):
        """org.freedesktop.DBus.Properties.PropertiesChanged"""
        # This method body intentionally left blank – the decorator does the work.
        pass


# ──────────────────────────────────────────────────────────────────────────
#  DESCRIPTOR IMPLEMENTATION (CCCD)
# ──────────────────────────────────────────────────────────────────────────

class ClientConfigDescriptor(dbus.service.Object, DBusPathMixin):
    """Implementation of the mandatory Client Characteristic Configuration Descriptor (CCCD)."""

    UUID = CCCD_UUID

    def __init__(self, bus, index: int, characteristic: Characteristic):
        self.path = f"{characteristic.get_path()}/desc{index}"
        super().__init__(bus, self.path)
        self.characteristic = characteristic
        # Default: notifications disabled
        self.value = [dbus.Byte(0), dbus.Byte(0)]

    # ------------------------------------------------------------------
    # Write / Read handlers
    # ------------------------------------------------------------------

    @dbus.service.method("org.bluez.GattDescriptor1", in_signature="aya{sv}")
    def WriteValue(self, value, options):  # noqa: N802 – DBus naming
        # [1,0] → on; [0,0] → off
        self.value = value
        on = (len(value) >= 2 and value[0] == 1)
        self.characteristic.notifying = on
        logger = self.characteristic  # reuse characteristic logger
        logger.info("Notifications %s via CCCD descriptor", "enabled" if on else "disabled")

    @dbus.service.method("org.bluez.GattDescriptor1", out_signature="aya{sv}")
    def ReadValue(self, options):  # noqa: N802 – DBus naming
        return self.value

    # ------------------------------------------------------------------
    # Properties helpers
    # ------------------------------------------------------------------

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):  # noqa: N802 – DBus naming
        if prop == "UUID":
            return dbus.String(self.UUID)
        if prop == "Characteristic":
            return self.characteristic.get_path()
        if prop == "Value":
            return dbus.Array(self.value, signature="y")
        return None

    @dbus.service.method(DBUS_PROP_IFACE, out_signature="a{sv}")
    def GetAll(self):  # noqa: N802 – DBus naming
        return {
            "UUID": self.UUID,
            "Characteristic": self.characteristic.get_path(),
            "Value": dbus.Array(self.value, signature="y"),
        }


# ──────────────────────────────────────────────────────────────────────────
#  LE ADVERTISEMENT HELPER
# ──────────────────────────────────────────────────────────────────────────

class Advertisement(dbus.service.Object, DBusPathMixin):
    """Simple LEAdvertisement1 implementation advertising the SyncSonic service UUID."""

    PATH_BASE = "/org/bluez/example/advertisement"

    def __init__(self, bus, index: int, advertising_type: str = "peripheral"):
        self.path            = self.PATH_BASE + str(index)
        self.bus             = bus
        self.ad_type         = advertising_type
        self.service_uuids   = [SERVICE_UUID]
        self.local_name      = "Sync-Sonic"
        self.include_tx_power= True
        self.discoverable    = True
        super().__init__(bus, self.path)
        log.info("Advertisement created at %s", self.path)

    # ------------------------------------------------------------------
    # Properties helpers
    # ------------------------------------------------------------------

    def get_properties(self) -> dict:
        return advertisement_props(
            ad_type=self.ad_type,
            service_uuids=self.service_uuids,
            local_name=self.local_name,
            include_tx_power=self.include_tx_power,
            discoverable=self.discoverable,
        )

    # ------------------------------------------------------------------
    # DBus methods – LEAdvertisement1 & Properties
    # ------------------------------------------------------------------

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, interface, prop):  # noqa: N802 – DBus naming
        return self.get_properties()["org.bluez.LEAdvertisement1"][prop]

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):  # noqa: N802 – DBus naming
        return self.get_properties()["org.bluez.LEAdvertisement1"]

    @dbus.service.method("org.bluez.LEAdvertisement1", in_signature="", out_signature="")
    def Release(self):  # noqa: N802 – DBus naming
        log.info("Advertisement released") 