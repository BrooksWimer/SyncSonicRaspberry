"""All project‑wide constants & enums live here."""
from enum import IntEnum
import os

# Getting reserved vaiable
reserved = os.getenv("RESERVED_HCI")
if not reserved:
    raise RuntimeError("RESERVED_HCI not set – cannot pick phone adapter")

# D-Bus names / interfaces ---------------------------------------------------
BLUEZ_SERVICE_NAME           = "org.bluez"
DBUS_OM_IFACE                = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE              = "org.freedesktop.DBus.Properties"

# Core adapter/device interfaces
ADAPTER_INTERFACE            = "org.bluez.Adapter1"
DEVICE_INTERFACE             = "org.bluez.Device1"

# GATT registration & runtime interfaces
GATT_MANAGER_IFACE           = "org.bluez.GattManager1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
GATT_SERVICE_IFACE           = "org.bluez.GattService1"
GATT_CHRC_IFACE              = "org.bluez.GattCharacteristic1"

# Agent interfaces
AGENT_INTERFACE              = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE      = "org.bluez.AgentManager1"
AGENT_MANAGER_IFACE          = "org.bluez.AgentManager1"

# Default agent object path
AGENT_PATH                   = "/com/syncsonic/pair_agent"

# BLE UUIDs ------------------------------------------------------------------
SERVICE_UUID                 = "19b10000-e8f2-537e-4f6c-d104768a1214"
CHARACTERISTIC_UUID          = "19b10001-e8f2-537e-4f6c-d104768a1217"

# Common profile/descriptor UUIDs -----------------------------------------
A2DP_UUID                    = "0000110b-0000-1000-8000-00805f9b34fb"  # Bluetooth A2DP Sink profile
CCCD_UUID                    = "00002902-0000-1000-8000-00805f9b34fb"  # Client Characteristic Configuration Descriptor

# Message types – converted to an Enum for type‑safety -----------------------
class Msg(IntEnum):
    PING                    = 0x01
    PONG                    = 0x02
    ERROR                   = 0x03
    SUCCESS                 = 0xF0
    FAILURE                 = 0xF1
    CONNECT_ONE             = 0x60
    DISCONNECT              = 0x61
    SET_LATENCY             = 0x62
    SET_VOLUME              = 0x63
    GET_PAIRED_DEVICES      = 0x64
    SET_MUTE                = 0x65
    ULTRASONIC_SYNC         = 0x67
    # Slice 4.2: mic-driven single-speaker auto-calibration
    CALIBRATE_SPEAKER       = 0x68
    # Slice 4.3: sequential calibration for every connected output filter
    CALIBRATE_ALL_SPEAKERS  = 0x69
    CONNECTION_STATUS_UPDATE= 0x70
    COORDINATOR_STATE       = 0x71  # Slice 3.6: 1 Hz per-speaker health snapshot
    COORDINATOR_EVENT       = 0x72  # Slice 3.6: edge-triggered soft_mute / state-change
    CALIBRATION_RESULT      = 0x73  # Slice 4.2: async calibration progress + final result
    SCAN_START      = 0x40
    SCAN_STOP       = 0x41
    SCAN_DEVICES    = 0x43
    WIFI_SCAN_START = 0x44
    WIFI_SCAN_STOP  = 0x45
    WIFI_SCAN_RESULTS = 0x46