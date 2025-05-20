from __future__ import annotations

import dbus, subprocess, os, json
from typing import Dict, Any
from syncsonic_ble.utils.constants import Msg, DBUS_PROP_IFACE, DBUS_OM_IFACE, DEVICE_INTERFACE, BLUEZ_SERVICE_NAME, ADAPTER_INTERFACE
from syncsonic_ble.helpers.pulseaudio_helpers import create_loopback
from syncsonic_ble.utils.logging_conf import get_logger
from syncsonic_ble.state_management.scan_manager import ScanManager

logger = get_logger(__name__)

def _encode(msg: Msg, payload: Dict[str, Any]):
    raw = json.dumps(payload).encode()
    out = [dbus.Byte(msg)] + [dbus.Byte(b) for b in raw]
    return out

# Each handler receives the Characteristic instance (self) and the parsed data dict.

def handle_ping(char, data):
    count = data.get("count", 0)
    return _encode(Msg.PONG, {"count": count})


def handle_connect_one(char, data):
    from syncsonic_ble.state_management.connection_manager import Intent
    service = char.connection_service
    tgt = data.get("targetSpeaker", {})
    mac = tgt.get("mac")
    if not mac:
        return _encode(Msg.ERROR, {"error": "Missing targetSpeaker.mac"})

    payload = {
        "mac": mac,
        "friendly_name": tgt.get("name", ""),
        "allowed": data.get("allowed", []),
    }
    logger.info("Queuing CONNECT_ONE %s", payload)
    if service:
        service.submit(Intent.CONNECT_ONE, payload)
    if char.device_manager:
        char.device_manager.connected.add(mac)
    return _encode(Msg.SUCCESS, {"queued": True})


def handle_disconnect(char, data):
    from syncsonic_ble.state_management.connection_manager import Intent
    service = char.connection_service
    mac = data.get("mac")
    if not mac:
        return _encode(Msg.ERROR, {"error": "Missing mac"})
    if service:
        service.submit(Intent.DISCONNECT, {"mac": mac})
    return _encode(Msg.SUCCESS, {"queued": True})


def handle_set_latency(char, data):
    mac = data.get("mac"); latency = data.get("latency")
    if mac is None or latency is None:
        return _encode(Msg.ERROR, {"error": "Missing mac/latency"})
    sink_prefix = f"bluez_sink.{mac.replace(':', '_')}"
    ok = create_loopback(sink_prefix, latency_ms=int(latency))
    if ok:
        return _encode(Msg.SUCCESS, {"latency": latency})
    return _encode(Msg.ERROR, {"error": "loopback failed"})


def handle_set_volume(char, data):
    mac = data.get("mac"); volume = data.get("volume")
    if mac is None or volume is None:
        return _encode(Msg.ERROR, {"error": "Missing mac/volume"})

    # balance is optional, defaults to centered (0.5)
    balance = float(data.get("balance", 0.5))

    # Clamp balance to [0.0, 1.0]
    balance = max(0.0, min(1.0, balance))

    volume = int(volume)

    # Compute left/right volumes based on balance
    if balance >= 0.5:
        left  = round(volume * (1 - balance) * 2)
        right = volume
    else:
        left  = volume
        right = round(volume * balance * 2)

    # Clamp output to 0–150%
    left  = min(max(left, 0), 150)
    right = min(max(right, 0), 150)

    sink_name = f"bluez_sink.{mac.replace(':', '_')}.a2dp_sink"
    result = subprocess.run([
        "pactl", "set-sink-volume", sink_name, f"{left}%", f"{right}%"
    ], capture_output=True, text=True)

    if result.returncode == 0:
        return _encode(Msg.SUCCESS, {"left": left, "right": right})
    return _encode(Msg.ERROR, {"error": result.stderr.strip() or "volume failed"})


def handle_get_paired(char, _):
    om = dbus.Interface(char.bus.get_object("org.bluez", "/"), DBUS_OM_IFACE)
    paired = {
        v.get("Address"): (v.get("Alias") or v.get("Name"))
        for _, ifs in om.GetManagedObjects().items()
        if (v := ifs.get(DEVICE_INTERFACE)) and v.get("Paired", False)
    }
    return _encode(Msg.SUCCESS, paired or {"message": "No devices"})


def handle_set_mute(char, data):
    mac = data.get("mac"); mute = data.get("mute")
    if mac is None or mute is None:
        return _encode(Msg.ERROR, {"error": "Missing mac/mute"})
    mac_fmt = mac.replace(":", "_")
    proc = subprocess.run(["pactl", "list", "sinks", "short"], capture_output=True, text=True)
    if proc.returncode != 0:
        return _encode(Msg.ERROR, {"error": "Cannot list sinks"})
    sink_name = next((l.split()[1] for l in proc.stdout.splitlines() if mac_fmt in l), None)
    if not sink_name:
        return _encode(Msg.ERROR, {"error": "sink not found"})
    flag = "1" if mute else "0"
    subprocess.run(["pactl", "set-sink-mute", sink_name, flag], check=True)
    return _encode(Msg.SUCCESS, {"mac": mac, "mute": mute})

# SCAN handlers ------------------------------------------------------------

def _scan_start(char, _):
    hci = os.getenv("RESERVED_HCI")
    adapter_path = f"/org/bluez/{hci}"
    try:
        obj = char.bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
        props = dbus.Interface(obj, DBUS_PROP_IFACE)
        adapter_mac = props.Get(ADAPTER_INTERFACE, "Address")
        logger.info("→ [SCAN_START] Found adapter %s (%s)", adapter_path, adapter_mac)
        char._scan_mgr = ScanManager()
        char._scan_mgr.ensure_discovery(adapter_mac)
        char.device_manager.scanning = True if char.device_manager else None
        char._scan_adapter_mac = adapter_mac
    except Exception as e:
        return _encode(Msg.ERROR, {"error": "Adapter not found"})
    return _encode(Msg.SUCCESS, {"scanning": True})


def _scan_stop(char, _):
    if not char._scan_mgr or not char._scan_adapter_mac:
        return _encode(Msg.ERROR, {"error": "Scan not active"})
    try:
        char._scan_mgr.release_discovery(char._scan_adapter_mac)
    except Exception:
        return _encode(Msg.ERROR, {"error": "Could not stop scan"})
    if char.device_manager:
        char.device_manager.scanning = False
    char._scan_mgr = None
    char._scan_adapter_mac = None
    return _encode(Msg.SUCCESS, {"scanning": False})

# -------------------------------------------------------------------------

def unknown_handler(char, _):
    return _encode(Msg.ERROR, {"error": "Unknown message"})

HANDLERS = {
    Msg.PING: handle_ping,
    Msg.CONNECT_ONE: handle_connect_one,
    Msg.DISCONNECT: handle_disconnect,
    Msg.SET_LATENCY: handle_set_latency,
    Msg.SET_VOLUME: handle_set_volume,
    Msg.GET_PAIRED_DEVICES: handle_get_paired,
    Msg.SET_MUTE: handle_set_mute,
    Msg.SCAN_START: _scan_start,
    Msg.SCAN_STOP: _scan_stop,
} 