from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict

import dbus

from syncsonic_ble.helpers.actuation import get_actuation_manager
from syncsonic_ble.helpers.adapter_helpers import is_device_on_reserved_adapter
from syncsonic_ble.helpers.device_type_helpers import is_sonos
from syncsonic_ble.helpers.pipewire_control_plane import publish_output_mix
from syncsonic_ble.state_management.scan_manager import ScanManager
from syncsonic_ble.telemetry import EventType
from syncsonic_ble.telemetry.event_writer import emit
from syncsonic_ble.utils.constants import (
    ADAPTER_INTERFACE,
    BLUEZ_SERVICE_NAME,
    DBUS_OM_IFACE,
    DBUS_PROP_IFACE,
    DEVICE_INTERFACE,
    Msg,
)
from syncsonic_ble.utils.logging_conf import get_logger

logger = get_logger(__name__)


def _encode(msg: Msg, payload: Dict[str, Any]):
    raw = json.dumps(payload).encode()
    return [dbus.Byte(msg)] + [dbus.Byte(byte) for byte in raw]


def _compact_single_output_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}

    compact: Dict[str, Any] = {}
    for key in (
        "mac",
        "delay_cmd_ms",
        "delay_applied_ms",
        "delay_line_applied_ms",
        "mode",
        "backend",
        "backend_reason",
    ):
        if key in snapshot:
            compact[key] = snapshot[key]
    return compact


def _feature_disabled(feature: str, message: str):
    return _encode(
        Msg.ERROR,
        {
            "error": message,
            "feature_disabled": True,
            "feature": feature,
        },
    )


def handle_ping(char, data):
    count = data.get("count", 0)
    return _encode(Msg.PONG, {"count": count})


def handle_connect_one(char, data):
    from syncsonic_ble.state_management.connection_manager import Intent

    service = char.connection_service
    target = data.get("targetSpeaker", {})
    mac = target.get("mac")
    if not mac:
        return _encode(Msg.ERROR, {"error": "Missing targetSpeaker.mac"})
    if is_sonos(mac):
        return _feature_disabled(
            "wifi_speakers",
            "Wi-Fi speaker support is disabled on the neutral foundation branch.",
        )

    payload = {
        "mac": mac,
        "friendly_name": target.get("name", ""),
        "allowed": data.get("allowed", []),
        "settings": data.get("settings", {}).get(mac, {}),
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
    if is_sonos(mac):
        return _feature_disabled(
            "wifi_speakers",
            "Wi-Fi speaker support is disabled on the neutral foundation branch.",
        )
    if service:
        service.submit(Intent.DISCONNECT, {"mac": mac})
    return _encode(Msg.SUCCESS, {"queued": True})


def handle_set_latency(char, data):
    mac = data.get("mac")
    latency = data.get("latency")
    if mac is None or latency is None:
        return _encode(Msg.ERROR, {"error": "Missing mac/latency"})
    if is_sonos(mac):
        return _feature_disabled(
            "wifi_speakers",
            "Wi-Fi speaker latency control is disabled on the neutral foundation branch.",
        )
    # Phone MAC must never enter the speaker delay control plane. The phone is
    # an A2DP source via the RESERVED_HCI adapter, not an output, so a delay
    # publish on its MAC produces a daemon poll loop that can never resolve a
    # bluez_output sink (observed on Pi as the 410 ms transport-sink-not-found
    # warning storm).
    if char.bus and is_device_on_reserved_adapter(char.bus, mac):
        logger.warning("Refusing SET_LATENCY for reserved-adapter device %s (phone MAC)", mac)
        return _encode(Msg.ERROR, {"error": "MAC is on the reserved adapter (phone), cannot apply output delay"})

    latency_ms = float(latency)
    emit(EventType.SET_LATENCY_REQUEST, {"mac": mac, "delay_ms": latency_ms})
    manager = get_actuation_manager()
    ok, snapshot = manager.apply_control_target(mac, delay_ms=latency_ms, rate_ppm=0.0, mode="manual")
    if ok:
        if char.connection_service:
            char.connection_service.loopbacks.add(mac.upper())
            logger.info("Added %s to loopbacks set after latency update", mac)
        return _encode(
            Msg.SUCCESS,
            {"latency": latency, "actuation": _compact_single_output_snapshot(snapshot)},
        )
    return _encode(Msg.ERROR, {"error": "stage delay update failed"})


def handle_set_volume(char, data):
    mac = data.get("mac")
    volume = data.get("volume")
    if mac is None or volume is None:
        return _encode(Msg.ERROR, {"error": "Missing mac/volume"})
    if is_sonos(mac):
        return _feature_disabled(
            "wifi_speakers",
            "Wi-Fi speaker volume control is disabled on the neutral foundation branch.",
        )
    # Same guard as handle_set_latency: the phone MAC is not an output, so
    # publishing a volume against it pollutes the control plane.
    if char.bus and is_device_on_reserved_adapter(char.bus, mac):
        logger.warning("Refusing SET_VOLUME for reserved-adapter device %s (phone MAC)", mac)
        return _encode(Msg.ERROR, {"error": "MAC is on the reserved adapter (phone), cannot apply output volume"})

    volume = int(volume)
    balance = float(data.get("balance", 0.5))
    balance = max(0.0, min(1.0, balance))
    if balance >= 0.5:
        left = round(volume * (1 - balance) * 2)
        right = volume
    else:
        left = volume
        right = round(volume * balance * 2)
    left = min(max(left, 0), 150)
    right = min(max(right, 0), 150)

    emit(EventType.SET_VOLUME_REQUEST, {
        "mac": mac,
        "volume": volume,
        "balance": balance,
        "left_percent": left,
        "right_percent": right,
    })
    publish_output_mix(mac, left_percent=left, right_percent=right)
    return _encode(Msg.SUCCESS, {"left": left, "right": right})


def handle_get_paired(char, _):
    om = dbus.Interface(char.bus.get_object("org.bluez", "/"), DBUS_OM_IFACE)
    paired = {
        value.get("Address"): (value.get("Alias") or value.get("Name"))
        for _, interfaces in om.GetManagedObjects().items()
        if (value := interfaces.get(DEVICE_INTERFACE)) and value.get("Paired", False)
    }
    return _encode(Msg.SUCCESS, paired or {"message": "No devices"})


def handle_set_mute(char, data):
    mac = data.get("mac")
    mute = data.get("mute")
    if mac is None or mute is None:
        return _encode(Msg.ERROR, {"error": "Missing mac/mute"})

    mac_fmt = mac.replace(":", "_")
    proc = subprocess.run(["pactl", "list", "sinks", "short"], capture_output=True, text=True)
    if proc.returncode != 0:
        return _encode(Msg.ERROR, {"error": "Cannot list sinks"})

    sink_name = next((line.split()[1] for line in proc.stdout.splitlines() if mac_fmt in line), None)
    if not sink_name:
        return _encode(Msg.ERROR, {"error": "sink not found"})

    subprocess.run(["pactl", "set-sink-mute", sink_name, "1" if mute else "0"], check=True)
    return _encode(Msg.SUCCESS, {"mac": mac, "mute": mute})


def _scan_start(char, _):
    hci = os.getenv("RESERVED_HCI")
    adapter_path = f"/org/bluez/{hci}"
    try:
        obj = char.bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
        props = dbus.Interface(obj, DBUS_PROP_IFACE)
        adapter_mac = props.Get(ADAPTER_INTERFACE, "Address")
        logger.info("[SCAN_START] Found adapter %s (%s)", adapter_path, adapter_mac)
        char._scan_mgr = ScanManager()
        char._scan_mgr.ensure_discovery(adapter_mac)
        if char.device_manager:
            char.device_manager.scanning = True
        char._scan_adapter_mac = adapter_mac
    except Exception:  # noqa: BLE001
        return _encode(Msg.ERROR, {"error": "Adapter not found"})
    return _encode(Msg.SUCCESS, {"scanning": True})


def _scan_stop(char, _):
    if not char._scan_mgr or not char._scan_adapter_mac:
        return _encode(Msg.ERROR, {"error": "Scan not active"})
    try:
        char._scan_mgr.release_discovery(char._scan_adapter_mac)
    except Exception:  # noqa: BLE001
        return _encode(Msg.ERROR, {"error": "Could not stop scan"})
    if char.device_manager:
        char.device_manager.scanning = False
    char._scan_mgr = None
    char._scan_adapter_mac = None
    return _encode(Msg.SUCCESS, {"scanning": False})


def handle_wifi_scan_start(char, _):
    return _feature_disabled(
        "wifi_speakers",
        "Wi-Fi speaker support is disabled on the neutral foundation branch.",
    )


def handle_ultrasonic_sync(char, _):
    return _feature_disabled(
        "ultrasonic_sync",
        "Ultrasonic auto-alignment is disabled on the neutral foundation branch.",
    )


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
    Msg.ULTRASONIC_SYNC: handle_ultrasonic_sync,
    Msg.SCAN_START: _scan_start,
    Msg.SCAN_STOP: _scan_stop,
    Msg.WIFI_SCAN_START: handle_wifi_scan_start,
}
