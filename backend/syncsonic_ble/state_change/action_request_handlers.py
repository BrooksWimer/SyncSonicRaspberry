from __future__ import annotations

import dbus, subprocess, os, json
import threading
from typing import Dict, Any
from syncsonic_ble.utils.constants import Msg, DBUS_PROP_IFACE, DBUS_OM_IFACE, DEVICE_INTERFACE, BLUEZ_SERVICE_NAME, ADAPTER_INTERFACE
from syncsonic_ble.helpers.actuation import get_actuation_manager
from syncsonic_ble.helpers.pipewire_control_plane import get_transport_base_ms, publish_output_mix
from syncsonic_ble.helpers.device_labels import register_device_label, format_device_label
from syncsonic_ble.helpers.device_type_helpers import is_sonos
from syncsonic_ble.utils.logging_conf import get_logger
from syncsonic_ble.state_management.scan_manager import ScanManager

logger = get_logger(__name__)

def _encode(msg: Msg, payload: Dict[str, Any]):
    raw = json.dumps(payload).encode()
    out = [dbus.Byte(msg)] + [dbus.Byte(b) for b in raw]
    return out


def _compact_actuation_payload(details: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(details, dict):
        return {}

    compact: Dict[str, Any] = {}
    for key in (
        "reason",
        "action",
        "offset_ms",
        "filtered_offset_ms",
        "controller_mode",
        "target_ratio_ppm",
        "adjusted_mac",
        "target_mac",
        "reference_mac",
        "target_delay_ms",
        "correction_step_ms",
        "applied",
        "setup_done",
        "target_onset_ms",
        "data_only",
        "counts",
    ):
        if key in details:
            compact[key] = details[key]

    states = details.get("states")
    if isinstance(states, dict):
        compact["speaker_count"] = len(states)

    controller_states = details.get("controller_states")
    if isinstance(controller_states, dict):
        compact["controller_count"] = len(controller_states)

    measured = details.get("measured")
    if isinstance(measured, list):
        compact["measured_count"] = len(measured)
    detections = details.get("detections")
    if isinstance(detections, list):
        compact["detection_count"] = len(detections)
    paired = details.get("paired")
    if isinstance(paired, list):
        compact["paired_count"] = len(paired)
    relative = details.get("relative")
    if isinstance(relative, list):
        compact["relative_count"] = len(relative)

    failed = details.get("failed")
    if isinstance(failed, list):
        compact["failed_count"] = len(failed)

    artifacts = details.get("artifacts")
    if isinstance(artifacts, dict):
        compact["artifact_files"] = {
            "wav": os.path.basename(str(artifacts.get("wav_path", ""))),
            "png": os.path.basename(str(artifacts.get("png_path", ""))),
            "json": os.path.basename(str(artifacts.get("json_path", ""))),
        }

    # Keep BLE notify payload bounded to avoid truncation/parse failures.
    try:
        encoded = json.dumps(compact, separators=(",", ":"), sort_keys=True)
        if len(encoded) > 420:
            compact = {
                "reason": compact.get("reason"),
                "setup_done": compact.get("setup_done"),
                "data_only": compact.get("data_only"),
                "counts": compact.get("counts"),
                "artifact_files": compact.get("artifact_files"),
                "detection_count": compact.get("detection_count"),
                "paired_count": compact.get("paired_count"),
            }
    except Exception:
        pass

    return compact


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

# Each handler receives the Characteristic instance (self) and the parsed data dict.

def handle_ping(char, data):
    count = data.get("count", 0)
    return _encode(Msg.PONG, {"count": count})


def handle_connect_one(char, data):
    from syncsonic_ble.state_management.connection_manager import Intent
    service = char.connection_service
    tgt = data.get("targetSpeaker", {})
    mac = tgt.get("mac")  # BT MAC or sonos:UID
    if not mac:
        return _encode(Msg.ERROR, {"error": "Missing targetSpeaker.mac"})

    payload = {
        "mac": mac,
        "friendly_name": tgt.get("name", ""),
        "allowed": data.get("allowed", []),
        "settings": data.get("settings", {}).get(mac, {}),
    }
    if payload["friendly_name"]:
        register_device_label(mac, payload["friendly_name"])
    logger.info("Queuing CONNECT_ONE %s", payload)
    if service:
        service.submit(Intent.CONNECT_ONE, payload)
    # Only add to BT connected set for Bluetooth devices; Wi‑Fi is updated in worker
    if char.device_manager and not is_sonos(mac):
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
    mac = data.get("mac")
    latency = data.get("latency")
    if mac is None or latency is None:
        return _encode(Msg.ERROR, {"error": "Missing mac/latency"})
    label = format_device_label(mac)
    if is_sonos(mac):
        # Sonos: store desired latency for future use; no-op for v1
        return _encode(Msg.SUCCESS, {"latency": latency})
    slider_latency_ms = max(0.0, float(latency))
    latency_ms = get_transport_base_ms() + slider_latency_ms
    manager = get_actuation_manager()
    ok, snapshot = manager.apply_control_target(mac, delay_ms=latency_ms, rate_ppm=0.0, mode="manual")
    if ok:
        if char.connection_service:
            char.connection_service.loopbacks.add(mac)
            logger.info("✅ Added %s to loopbacks set after latency update", label)
        return _encode(Msg.SUCCESS, {"latency": latency, "actuation": _compact_single_output_snapshot(snapshot)})
    return _encode(Msg.ERROR, {"error": "stage delay update failed"})


def handle_set_volume(char, data):
    mac = data.get("mac")
    volume = data.get("volume")
    if mac is None or volume is None:
        return _encode(Msg.ERROR, {"error": "Missing mac/volume"})

    volume = int(volume)

    if is_sonos(mac):
        try:
            from syncsonic_ble.helpers.sonos_controller import set_volume as sonos_set_volume
            if sonos_set_volume(mac, volume):
                return _encode(Msg.SUCCESS, {"volume": volume})
            return _encode(Msg.ERROR, {"error": "Sonos volume failed"})
        except Exception as e:
            logger.exception("Sonos set_volume failed: %s", e)
            return _encode(Msg.ERROR, {"error": str(e)})

    # Bluetooth: balance and left/right
    balance = float(data.get("balance", 0.5))
    balance = max(0.0, min(1.0, balance))
    if balance >= 0.5:
        left  = round(volume * (1 - balance) * 2)
        right = volume
    else:
        left  = volume
        right = round(volume * balance * 2)
    left  = min(max(left, 0), 150)
    right = min(max(right, 0), 150)

    publish_output_mix(mac, left_percent=left, right_percent=right)
    return _encode(Msg.SUCCESS, {"left": left, "right": right})


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


def _run_wifi_scan_worker(char):
    """Run Sonos discovery and notify WIFI_SCAN_RESULTS."""
    try:
        from syncsonic_ble.state_management.scan_manager import scan_wifi_sonos
        wifi_devices = scan_wifi_sonos(timeout=5)
        char.send_notification(Msg.WIFI_SCAN_RESULTS, {"wifi_devices": wifi_devices})
    except Exception as e:
        logger.exception("[WiFiScan] Sonos discovery failed: %s", e)
        char.send_notification(Msg.FAILURE, {"error": str(e), "wifi_scan": True})


def handle_wifi_scan_start(char, _):
    """Start Wi‑Fi (Sonos) discovery; results sent via WIFI_SCAN_RESULTS notification."""
    t = threading.Thread(target=_run_wifi_scan_worker, args=(char,), daemon=True)
    t.start()
    return _encode(Msg.SUCCESS, {"queued": True, "message": "Wi‑Fi scan started"})


def _run_ultrasonic_sync_worker(char):
    """Run startup probe phase-1 (data-only) in background and notify when done."""
    try:
        from syncsonic_ble.helpers.latency_setup import run_startup_probe_phase1

        connected_ids = []
        if getattr(char, "device_manager", None):
            connected_ids = list(char.device_manager._all_connected())
        
        def _request_phone_beep(step: dict) -> bool:
            char.send_notification(Msg.SUCCESS, {"probe_beep_request": step})
            return True

        ok, details = run_startup_probe_phase1(
            connected_ids=connected_ids,
            playback_callback=_request_phone_beep,
        )
        char.send_notification(Msg.SUCCESS, {
            "ultrasonic_sync_done": True,
            "success": ok,
            "message": "Startup probe completed." if ok else "Startup probe completed with gaps.",
            "actuation": _compact_actuation_payload(details),
        })
    except Exception as e:
        logger.exception("Startup probe phase-1 failed: %s", e)
        char.send_notification(Msg.SUCCESS, {
            "ultrasonic_sync_done": True,
            "success": False,
            "message": str(e),
        })


def handle_ultrasonic_sync(char, _):
    """Queue one end-to-end setup calibration cycle; result is sent via notification."""
    t = threading.Thread(target=_run_ultrasonic_sync_worker, args=(char,), daemon=True)
    t.start()
    return _encode(Msg.SUCCESS, {"queued": True, "message": "Startup probe started."})


def handle_startup_probe_begin(_char, data):
    from syncsonic_ble.helpers.latency_setup import begin_frontend_probe_session

    raw_targets = data.get("targets", [])
    targets = [str(x) for x in raw_targets] if isinstance(raw_targets, list) else []
    ok, details = begin_frontend_probe_session(targets)
    if ok:
        return _encode(Msg.SUCCESS, {"probe_begin": True, **details})
    return _encode(Msg.ERROR, {"probe_begin": False, **details})


def handle_startup_probe_step(_char, data):
    from syncsonic_ble.helpers.latency_setup import mark_frontend_probe_step

    target_id = data.get("target_id")
    speaker = data.get("speaker")
    ok, details = mark_frontend_probe_step(str(target_id or ""), str(speaker or ""))
    if ok:
        return _encode(Msg.SUCCESS, {"probe_step": True, **details})
    return _encode(Msg.ERROR, {"probe_step": False, **details})


def _run_startup_probe_finish_worker(char):
    from syncsonic_ble.helpers.latency_setup import finish_frontend_probe_session

    try:
        ok, details = finish_frontend_probe_session()
        char.send_notification(Msg.SUCCESS, {
            "ultrasonic_sync_done": True,
            "success": ok,
            "message": "Startup probe completed." if ok else "Startup probe completed with gaps.",
            "actuation": _compact_actuation_payload(details),
        })
    except Exception as e:
        logger.exception("Startup probe finish failed: %s", e)
        char.send_notification(Msg.SUCCESS, {
            "ultrasonic_sync_done": True,
            "success": False,
            "message": str(e),
        })


def handle_startup_probe_finish(char, _data):
    t = threading.Thread(target=_run_startup_probe_finish_worker, args=(char,), daemon=True)
    t.start()
    return _encode(Msg.SUCCESS, {"queued": True, "message": "Startup probe finish queued."})


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
    Msg.ULTRASONIC_SYNC: handle_ultrasonic_sync,
    Msg.STARTUP_PROBE_BEGIN: handle_startup_probe_begin,
    Msg.STARTUP_PROBE_STEP: handle_startup_probe_step,
    Msg.STARTUP_PROBE_FINISH: handle_startup_probe_finish,
    Msg.SCAN_START: _scan_start,
    Msg.SCAN_STOP: _scan_stop,
    Msg.WIFI_SCAN_START: handle_wifi_scan_start,
} 
