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

    payload = {
        "mac": mac,
        "friendly_name": target.get("name", ""),
        "allowed": data.get("allowed", []),
        "settings": data.get("settings", {}).get(mac, {}),
    }
    logger.info("Queuing CONNECT_ONE %s", payload)
    if service:
        service.submit(Intent.CONNECT_ONE, payload)

    # Sonos device IDs are tracked separately from BT MACs; the device_manager
    # union will surface them via _all_connected.
    if char.device_manager:
        if is_sonos(mac):
            char.device_manager.add_wifi_connected(mac)
        else:
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
    if char.device_manager and is_sonos(mac):
        char.device_manager.remove_wifi_connected(mac)
    return _encode(Msg.SUCCESS, {"queued": True})


def handle_set_latency(char, data):
    mac = data.get("mac")
    latency = data.get("latency")
    if mac is None or latency is None:
        return _encode(Msg.ERROR, {"error": "Missing mac/latency"})
    if is_sonos(mac):
        # Sonos has no per-device delay filter under the current architecture
        # (all Wi-Fi outputs share one Icecast stream from virtual_out.monitor),
        # so SET_LATENCY for a Sonos is a UI persistence ack only. The frontend
        # uses this to store the calibration anchor lag back onto the slider.
        try:
            latency_ms = float(latency)
        except (TypeError, ValueError):
            return _encode(Msg.ERROR, {"error": "Invalid latency value"})
        emit(EventType.SET_LATENCY_REQUEST, {
            "mac": mac, "delay_ms": latency_ms, "transport": "wifi",
        })
        return _encode(Msg.SUCCESS, {"latency": latency_ms, "transport": "wifi"})
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
        try:
            from syncsonic_ble.helpers import sonos_controller
        except ImportError as exc:
            logger.exception("sonos_controller import failed")
            return _encode(Msg.ERROR, {"error": f"sonos module unavailable: {exc}"})
        try:
            vol = int(volume)
        except (TypeError, ValueError):
            return _encode(Msg.ERROR, {"error": "Invalid volume value"})
        ok = sonos_controller.set_volume(mac, vol)
        emit(EventType.SET_VOLUME_REQUEST, {
            "mac": mac, "volume": vol, "transport": "wifi", "ok": bool(ok),
        })
        if not ok:
            return _encode(Msg.ERROR, {"error": "sonos set_volume failed", "mac": mac})
        return _encode(Msg.SUCCESS, {"volume": vol, "transport": "wifi"})
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
    """Run a one-shot Sonos discovery on a daemon thread and stream each
    found device back as a WIFI_SCAN_RESULTS notification. Idempotent: a
    scan already in flight is left to finish."""
    import threading

    if getattr(char, "_wifi_scan_in_flight", False):
        return _encode(Msg.SUCCESS, {"scanning": True, "already": True})

    def _runner():
        try:
            from syncsonic_ble.state_management.scan_manager import scan_wifi_sonos
            devices = scan_wifi_sonos(timeout=6)
            for device in devices:
                try:
                    char.send_notification(Msg.WIFI_SCAN_RESULTS, {"device": device})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("WIFI_SCAN_RESULTS notify failed: %s", exc)
            char.send_notification(Msg.WIFI_SCAN_RESULTS, {"phase": "complete", "count": len(devices)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Wi-Fi scan thread crashed: %s", exc)
        finally:
            char._wifi_scan_in_flight = False

    char._wifi_scan_in_flight = True
    threading.Thread(target=_runner, name="syncsonic-wifi-scan", daemon=True).start()
    return _encode(Msg.SUCCESS, {"scanning": True})


def handle_wifi_scan_stop(char, _):
    """Wi-Fi scan is one-shot, so this is just an idempotent ack (kept for
    parity with BT SCAN_STOP so the frontend can use the same UI flow)."""
    return _encode(Msg.SUCCESS, {"scanning": False})


def handle_ultrasonic_sync(char, _):
    return _feature_disabled(
        "ultrasonic_sync",
        "Ultrasonic auto-alignment is disabled on the neutral foundation branch.",
    )


def handle_calibrate_speaker(char, data):
    """Slice 4.2 / startup tune: mic-driven single-speaker calibration.

    Optional JSON keys:
      calibration_mode / mode: \"music\" (default) or \"startup_tune\"
        \"startup_tune\" plays the built-in chirp into ``virtual_out``
        during capture (pause phone audio first for a clean reference tap).
    """
    mac = data.get("mac")
    if not mac:
        return _encode(Msg.ERROR, {"error": "Missing mac"})
    if is_sonos(mac):
        # Per-Sonos calibration is not exposed: Wi-Fi alignment is anchored
        # measurement during CALIBRATE_ALL_SPEAKERS (BT speakers are pulled
        # to match the Wi-Fi anchor lag). A solo Sonos has nothing to align
        # to, so this is intentionally a no-op error.
        return _encode(
            Msg.ERROR,
            {"error": "Single-speaker calibration is not available for Wi-Fi outputs; use Align All."},
        )
    # Phone MAC must never enter the calibration path - the phone is
    # an A2DP source, not an output, and calibration would ask the
    # actuation manager to publish a delay against an adapter that
    # has no bluez_output sink. Same guard pattern as
    # handle_set_latency / handle_set_volume.
    if char.bus and is_device_on_reserved_adapter(char.bus, mac):
        logger.warning(
            "Refusing CALIBRATE_SPEAKER for reserved-adapter device %s (phone MAC)",
            mac,
        )
        return _encode(
            Msg.ERROR,
            {"error": "MAC is on the reserved adapter (phone), cannot calibrate"},
        )

    target_total_ms = float(data.get("target_total_ms", 500.0))
    capture_duration_sec = float(data.get("capture_duration_sec", 6.0))

    raw_mode = data.get("calibration_mode", data.get("mode", "music"))
    if raw_mode not in ("music", "startup_tune"):
        return _encode(
            Msg.ERROR,
            {"error": f"invalid calibration_mode {raw_mode!r}; use music or startup_tune"},
        )
    calibration_mode = "startup_tune" if raw_mode == "startup_tune" else "music"

    # Lazy import: keeps the GLib mainloop process startup path free
    # of scipy / numpy cost when no calibration ever runs.
    try:
        from measurement.calibrate_one import calibrate_speaker_async
    except ImportError as exc:
        logger.exception("calibrate_speaker_async import failed")
        return _encode(
            Msg.ERROR,
            {"error": f"calibration module unavailable: {exc}"},
        )

    def _push(_phase: str, payload: Dict[str, Any]) -> None:
        # The Characteristic's send_notification is already known to
        # be safe from worker threads (existing ConnectionService and
        # DeviceManager use the same pattern). Failures inside the
        # send are caught by the calibrator's own try/except, so we
        # don't need to wrap them here.
        char.send_notification(Msg.CALIBRATION_RESULT, payload)

    calibrate_speaker_async(
        target_mac=mac,
        on_event=_push,
        target_total_ms=target_total_ms,
        capture_duration_sec=capture_duration_sec,
        calibration_mode=calibration_mode,
    )
    return _encode(
        Msg.SUCCESS,
        {
            "queued": True,
            "mac": mac,
            "target_total_ms": target_total_ms,
            "capture_duration_sec": capture_duration_sec,
            "calibration_mode": calibration_mode,
        },
    )


def handle_calibrate_all_speakers(char, data):
    """Slice 4.3: run calibration once per connected output (sequential)."""
    try:
        from measurement.calibrate_sequence import calibrate_all_speakers_async
        from measurement.calibrate_one import CalibrationMode
    except ImportError as exc:
        logger.exception("calibrate_sequence import failed")
        return _encode(
            Msg.ERROR,
            {"error": f"calibration module unavailable: {exc}"},
        )

    raw_mode = data.get("calibration_mode", data.get("mode", "startup_tune"))
    if raw_mode not in ("music", "startup_tune"):
        return _encode(
            Msg.ERROR,
            {"error": f"invalid calibration_mode {raw_mode!r}; use music or startup_tune"},
        )
    calibration_mode: CalibrationMode = "startup_tune" if raw_mode == "startup_tune" else "music"

    target_total_ms = float(data.get("target_total_ms", 500.0))
    capture_duration_sec = data.get("capture_duration_sec")
    cont_raw = data.get("continue_on_failure", True)
    continue_on_failure = cont_raw if isinstance(cont_raw, bool) else str(cont_raw).lower() in (
        "1",
        "true",
        "yes",
    )

    def _push(_phase: str, payload: Dict[str, Any]) -> None:
        char.send_notification(Msg.CALIBRATION_RESULT, payload)

    wifi_device_ids: list[str] = []
    if char.device_manager:
        wifi_device_ids = sorted(char.device_manager.wifi_connected)

    calibrate_all_speakers_async(
        char.bus,
        _push,
        calibration_mode=calibration_mode,
        target_total_ms=target_total_ms,
        capture_duration_sec=float(capture_duration_sec) if capture_duration_sec is not None else None,
        continue_on_failure=continue_on_failure,
        wifi_device_ids=wifi_device_ids,
    )
    return _encode(
        Msg.SUCCESS,
        {
            "queued": True,
            "calibration_mode": calibration_mode,
            "target_total_ms": target_total_ms,
            "capture_duration_sec": capture_duration_sec,
        },
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
    Msg.CALIBRATE_SPEAKER: handle_calibrate_speaker,
    Msg.CALIBRATE_ALL_SPEAKERS: handle_calibrate_all_speakers,
    Msg.SCAN_START: _scan_start,
    Msg.SCAN_STOP: _scan_stop,
    Msg.WIFI_SCAN_START: handle_wifi_scan_start,
    Msg.WIFI_SCAN_STOP: handle_wifi_scan_stop,
}
