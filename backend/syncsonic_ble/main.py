"""Unified entrypoint for the SyncSonic BLE runtime."""

from __future__ import annotations

import os
import subprocess
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from syncsonic_ble.helpers.adapter_helpers import (
    find_adapter,
    get_reserved_advertising_manager,
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
from syncsonic_ble.coordinator.coordinator import build_and_start_coordinator
from syncsonic_ble.state_management.bus_manager import get_bus
from syncsonic_ble.state_management.connection_manager import ConnectionService
from syncsonic_ble.state_management.device_manager import DeviceManager
from syncsonic_ble.telemetry.collector import build_default_collector, set_collector
from syncsonic_ble.utils.constants import (
    ADAPTER_INTERFACE,
    AGENT_MANAGER_INTERFACE,
    AGENT_PATH,
    BLUEZ_SERVICE_NAME,
    CHARACTERISTIC_UUID,
    DBUS_PROP_IFACE,
    GATT_MANAGER_IFACE,
    SERVICE_UUID,
    reserved,
)
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)


def main() -> None:
    setup_audio_server()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = get_bus()
    set_bus(bus)

    adapter_path, _adapter = find_adapter(reserved)
    if not adapter_path:
        log.error("No Bluetooth adapter found, aborting")
        sys.exit(1)
    log.info("Using primary adapter: %s", adapter_path)

    try:
        subprocess.run(
            ["hciconfig", reserved, "name", "SyncSonic"],
            check=True,
            capture_output=True,
            timeout=5,
        )
        log.info("Set %s controller name to SyncSonic", reserved)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("hciconfig name failed: %s", exc)

    try:
        adapter_obj = bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
        props = dbus.Interface(adapter_obj, DBUS_PROP_IFACE)
        props.Set(ADAPTER_INTERFACE, "Alias", dbus.String("SyncSonic"))
        log.info("Set adapter Alias to SyncSonic")
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not set adapter Alias: %s", exc)

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

    cccd = ClientConfigDescriptor(bus, 0, char)
    if hasattr(char, "descriptors"):
        char.descriptors.append(cccd)

    app = Application(bus)
    app.add_service(gatt_service)

    gatt_mgr = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, adapter_path),
        GATT_MANAGER_IFACE,
    )
    _reserved_adapter_path, ad_mgr = get_reserved_advertising_manager(bus)
    adv = Advertisement(bus, 0)

    gatt_mgr.RegisterApplication(
        app.get_path(),
        {},
        reply_handler=lambda: log.info("GATT application registered on %s", adapter_path),
        error_handler=lambda err: log.error("GATT registration error on %s: %s", adapter_path, err),
    )
    ad_mgr.RegisterAdvertisement(
        adv.get_path(),
        {},
        reply_handler=lambda: log.info(
            "Advertisement active on adapter %s",
            os.getenv("RESERVED_HCI"),
        ),
        error_handler=lambda err: log.error(
            "Advertisement error on %s: %s",
            os.getenv("RESERVED_HCI"),
            err,
        ),
    )

    # Slice 1 telemetry collector. Background daemon thread; shares the
    # service's SystemBus so we don't open a second BlueZ connection.
    # Failures inside the collector are isolated and never break the
    # audio service.
    try:
        collector = build_default_collector(bus=bus)
        collector.start()
        set_collector(collector)
    except Exception as exc:  # noqa: BLE001
        log.warning("Telemetry collector failed to start, continuing without it: %s", exc)
        collector = None

    # Slice 3 System Coordinator. Daemon thread that observes every
    # live pw_delay_filter via its Unix socket. Observation-only in
    # commit 3.1; subsequent commits add bounded rate adjustment
    # (3.2), system-wide hold (3.3), soft-mute on transport failure
    # (3.4), and RSSI-aware preemptive soft-mute (3.5). Like the
    # collector, failure here must never break the audio service.
    try:
        coordinator = build_and_start_coordinator()
    except Exception as exc:  # noqa: BLE001
        log.warning("Coordinator failed to start, continuing without it: %s", exc)
        coordinator = None

    log.info("SyncSonic BLE server ready, service UUID %s", SERVICE_UUID)
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        log.info("Server stopped by user")
    finally:
        if coordinator is not None:
            try:
                coordinator.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("Coordinator stop failed: %s", exc)
        if collector is not None:
            try:
                collector.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("Telemetry collector stop failed: %s", exc)


if __name__ == "__main__":
    main()
