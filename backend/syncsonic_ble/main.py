"""Unified entry-point that bootstraps BLE GATT, ConnectionService and
runs the single GLib MainLoop.  Replaces the previous gatt_server,
event_pump and svc_singleton helpers."""

import sys, os, dbus, dbus.service, dbus.mainloop.glib
from gi.repository import GLib

# First-party modules -------------------------------------------------------
from syncsonic_ble.utils.logging_conf import get_logger
from syncsonic_ble.helpers.pulseaudio_helpers import setup_pulseaudio
from syncsonic_ble.utils.constants import (
    BLUEZ_SERVICE_NAME, SERVICE_UUID, CHARACTERISTIC_UUID, GATT_MANAGER_IFACE,
    LE_ADVERTISING_MANAGER_IFACE, AGENT_MANAGER_INTERFACE, AGENT_PATH, reserved
)
from syncsonic_ble.state_management.bus_manager import get_bus

# BlueZ / BLE helpers -------------------------------------------------------
from syncsonic_ble.infra.connection_agent import PhonePairingAgent, CAPABILITY
from syncsonic_ble.state_management.device_manager import DeviceManager
from syncsonic_ble.infra.gatt_service import (
    Characteristic,
    GattService,
    Application,
    ClientConfigDescriptor,
    Advertisement,
)
from syncsonic_ble.helpers.adapter_helpers import find_adapter, set_bus, get_reserved_advertising_manager

# High-level orchestration --------------------------------------------------
from syncsonic_ble.state_management.connection_manager import ConnectionService

log = get_logger(__name__)


# ───────────────────────────────────────────────────────────────────────────
#  Bootstrap helpers
# ───────────────────────────────────────────────────────────────────────────


def main():
    """Initialise everything then enter the single GLib main loop."""

    # 1) Audio & logging ----------------------------------------------------
    setup_pulseaudio()

    # 2) D-Bus / GLib integration -----------------------------------------
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = get_bus()        # singleton, offers pydbus-like helpers
    set_bus(bus)           # for legacy helpers

    # 3) BlueZ adapter selection -------------------------------------------
    adapter_path, adapter = find_adapter(reserved)
    if not adapter_path:
        log.error("No Bluetooth adapter found – aborting")
        sys.exit(1)
    log.info("🎛️  Using primary adapter: %s", adapter_path)

    # 4) Pairing agent ------------------------------------------------------
    PhonePairingAgent(bus, AGENT_PATH)  # object lives as long as program
    agent_mgr = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, "/org/bluez"),
        AGENT_MANAGER_INTERFACE,
    )
    agent_mgr.RegisterAgent(AGENT_PATH, CAPABILITY)
    agent_mgr.RequestDefaultAgent(AGENT_PATH)
    log.info("🤝 Pairing agent registered at %s", AGENT_PATH)

    # 5) Runtime services ---------------------------------------------------
    conn_service = ConnectionService()              # worker thread inside

    dev_mgr = DeviceManager(bus, adapter_path)

    gatt_service = GattService(bus, 0, SERVICE_UUID, primary=True)
    char = Characteristic(
        bus,
        0,
        CHARACTERISTIC_UUID,
        ["read", "write", "write-without-response", "notify"],
        gatt_service,
    )
    gatt_service.add_characteristic(char)

    # wire everything together
    dev_mgr.attach_characteristic(char)
    char.set_device_manager(dev_mgr)
    char.set_connection_service(conn_service)
    conn_service._char = char  # allow service to push BLE notifications

    # CCCD descriptor -------------------------------------------------------
    cccd = ClientConfigDescriptor(bus, 0, char)
    if hasattr(char, "descriptors"):
        char.descriptors.append(cccd)

    # Build full application tree ------------------------------------------
    app = Application(bus)
    app.add_service(gatt_service)

    # 6) Register GATT application & advertisement -------------------------
    gatt_mgr = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, adapter_path), GATT_MANAGER_IFACE)
    adapter_path_reserved, ad_mgr = get_reserved_advertising_manager(bus)
    adv = Advertisement(bus, 0)

    gatt_mgr.RegisterApplication(
        app.get_path(),
        {},
        reply_handler=lambda: log.info("✅ GATT application registered on %s", adapter_path),
        error_handler=lambda e: log.error("GATT registration error on %s: %s", adapter_path, e),
    )
    ad_mgr.RegisterAdvertisement(
        adv.get_path(),
        {},
        reply_handler=lambda: log.info("✅ Advertisement active on adapter %s", os.getenv("RESERVED_HCI")),
        error_handler=lambda e: log.error("Advertisement error on %s: %s", os.getenv("RESERVED_HCI"), e),
    )

    # 7) Enter main loop ----------------------------------------------------
    log.info("🚀 SyncSonic BLE server ready – service UUID %s", SERVICE_UUID)
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        log.info("🛑 Server stopped by user")


# allow `python -m syncsonic_ble` -------------------------------------------

if __name__ == "__main__":
    main()