"""All project-wide constants and enums live here."""

from enum import IntEnum
import os

# Reserved adapter settings:
# - `reserved` keeps legacy HCI lookup for existing call sites.
# - `reserved_mac` enables stable reservation by controller MAC.
reserved = os.getenv("RESERVED_HCI")
reserved_mac = os.getenv("RESERVED_ADAPTER_MAC")
if not reserved and not reserved_mac:
    raise RuntimeError("Either RESERVED_ADAPTER_MAC or RESERVED_HCI must be set")

# D-Bus names / interfaces
BLUEZ_SERVICE_NAME = "org.bluez"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"

# Core adapter/device interfaces
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEVICE_INTERFACE = "org.bluez.Device1"

# GATT registration & runtime interfaces
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"

# Agent interfaces
AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"

# Default agent object path
AGENT_PATH = "/com/syncsonic/pair_agent"

# BLE UUIDs
SERVICE_UUID = "19b10000-e8f2-537e-4f6c-d104768a1214"
CHARACTERISTIC_UUID = "19b10001-e8f2-537e-4f6c-d104768a1217"

# Common profile/descriptor UUIDs
A2DP_UUID = "0000110b-0000-1000-8000-00805f9b34fb"  # Bluetooth A2DP Sink
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"  # Client Characteristic Configuration Descriptor


class Msg(IntEnum):
    PING = 0x01
    PONG = 0x02
    ERROR = 0x03
    SUCCESS = 0xF0
    FAILURE = 0xF1
    CONNECT_ONE = 0x60
    DISCONNECT = 0x61
    SET_LATENCY = 0x62
    SET_VOLUME = 0x63
    GET_PAIRED_DEVICES = 0x64
    SET_MUTE = 0x65
    ULTRASONIC_SYNC = 0x67
    STARTUP_PROBE_BEGIN = 0x68
    STARTUP_PROBE_STEP = 0x69
    STARTUP_PROBE_FINISH = 0x6A
    CONNECTION_STATUS_UPDATE = 0x70
    SCAN_START = 0x40
    SCAN_STOP = 0x41
    SCAN_DEVICES = 0x43
    WIFI_SCAN_START = 0x44
    WIFI_SCAN_STOP = 0x45
    WIFI_SCAN_RESULTS = 0x46

