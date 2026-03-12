"""Unified entry-point that bootstraps BLE GATT, ConnectionService and
runs the single GLib MainLoop."""

import os
import subprocess
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from syncsonic_ble.helpers.adapter_helpers import (
    get_reserved_advertising_manager,
    resolve_reserved_adapter,
    set_bus,
)
from syncsonic_ble.helpers.pulseaudio_helpers import setup_audio_server
from syncsonic_ble.infra.connection_agent import CAPABILITY, PhonePairingAgent
from syncsonic_ble.infra.gatt_service import (
    Advertisement,
    Application,
    Characteristic,
    ClientConfigDescriptor,
    GattService,
)
from syncsonic_ble.state_management.bus_manager import get_bus
from syncsonic_ble.state_management.connection_manager import ConnectionService
from syncsonic_ble.state_management.device_manager import DeviceManager
from syncsonic_ble.utils.constants import (
    ADAPTER_INTERFACE,
    AGENT_MANAGER_INTERFACE,
    AGENT_PATH,
    BLUEZ_SERVICE_NAME,
    CHARACTERISTIC_UUID,
    DBUS_PROP_IFACE,
    GATT_MANAGER_IFACE,
    SERVICE_UUID,
)
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)


def main():
    """Initialise everything then enter the single GLib main loop."""
    runtime = os.getenv("SYNCSONIC_AUDIO_RUNTIME", "unknown")
    backend = os.getenv("SYNCSONIC_ACTUATION_BACKEND", "unknown")
    log.info("Startup audio runtime=%s actuation_backend=%s", runtime, backend)

    setup_audio_server()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = get_bus()
    set_bus(bus)

    try:
        reserved_adapter = resolve_reserved_adapter(bus)
    except Exception as exc:
        log.error("Failed to resolve reserved phone adapter: %s", exc)
        sys.exit(1)

    adapter_path = reserved_adapter["path"]
    reserved_hci = reserved_adapter["hci"]
    reserved_mac = reserved_adapter["address"]
    log.info(
        "Using reserved phone adapter: path=%s hci=%s mac=%s source=%s",
        adapter_path,
        reserved_hci,
        reserved_mac,
        reserved_adapter["source"],
    )

    try:
        subprocess.run(
            ["hciconfig", reserved_hci, "name", "SyncSonic"],
            check=True,
            capture_output=True,
            timeout=5,
        )
        log.info("Set %s controller name to SyncSonic (hciconfig)", reserved_hci)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("hciconfig name failed (phone may show other name): %s", exc)

    try:
        adapter_obj = bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
        props = dbus.Interface(adapter_obj, DBUS_PROP_IFACE)
        props.Set(ADAPTER_INTERFACE, "Alias", dbus.String("SyncSonic"))
        log.info("Set adapter Alias to SyncSonic (BlueZ)")
    except Exception as exc:
        log.warning("Could not set adapter Alias (phone may see wrong name): %s", exc)

    PhonePairingAgent(bus, AGENT_PATH)
    agent_mgr = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, "/org/bluez"),
        AGENT_MANAGER_INTERFACE,
    )
    agent_mgr.RegisterAgent(AGENT_PATH, CAPABILITY)
    agent_mgr.RequestDefaultAgent(AGENT_PATH)
    log.info("Pairing agent registered at %s", AGENT_PATH)

    conn_service = ConnectionService()
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

    dev_mgr.attach_characteristic(char)
    char.set_device_manager(dev_mgr)
    char.set_connection_service(conn_service)
    conn_service._char = char
    conn_service.set_device_manager(dev_mgr)

    cccd = ClientConfigDescriptor(bus, 0, char)
    if hasattr(char, "descriptors"):
        char.descriptors.append(cccd)

    app = Application(bus)
    app.add_service(gatt_service)

    gatt_mgr = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, adapter_path), GATT_MANAGER_IFACE)
    adv_adapter_path, ad_mgr = get_reserved_advertising_manager(bus)
    if adv_adapter_path != adapter_path:
        log.warning(
            "Reserved adapter mismatch: gatt_path=%s adv_path=%s",
            adapter_path,
            adv_adapter_path,
        )
    adv = Advertisement(bus, 0)

    gatt_mgr.RegisterApplication(
        app.get_path(),
        {},
        reply_handler=lambda: log.info("GATT application registered on %s", adapter_path),
        error_handler=lambda e: log.error("GATT registration error on %s: %s", adapter_path, e),
    )
    ad_mgr.RegisterAdvertisement(
        adv.get_path(),
        {},
        reply_handler=lambda: log.info(
            "Advertisement active on adapter %s (%s)", reserved_hci, reserved_mac
        ),
        error_handler=lambda e: log.error("Advertisement error on %s: %s", reserved_hci, e),
    )

    log.info("SyncSonic BLE server ready - service UUID %s", SERVICE_UUID)
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        log.info("Server stopped by user")


if __name__ == "__main__":
    main()

