# connection_service.py
"""connection_service
====================
Thread‑safe orchestrator that owns **all** Bluetooth logic:

* Maintains the *expected speaker* set.
* Uses :pyclass:`scan_manager.ScanManager` to serialise discovery.
* Runs **one** background worker thread that consumes intents from a
  `queue.Queue` – so every BlueZ call happens in that single thread.
* Can be driven by any transport: Flask today, BLE tomorrow.
is it working?
"""

from __future__ import annotations

import threading
from enum import Enum, auto
from queue import Queue, Empty
from typing import Dict, List, Optional, Tuple

from syncsonic_ble.state_management.bus_manager import get_bus
from syncsonic_ble.state_management.scan_manager import ScanManager
from syncsonic_ble.state_change.action_planning import connect_one_plan
from syncsonic_ble.state_change.action_functions import (
    disconnect_device_dbus,
    connect_device_dbus,
    pair_device_dbus,
    trust_device_dbus,
    remove_device_dbus,
)
from syncsonic_ble.helpers.actuation import get_actuation_manager
from syncsonic_ble.helpers.pipewire_control_plane import (
    get_transport_base_ms,
    publish_output_mix,
    publish_transport_profile,
)
from syncsonic_ble.helpers.device_type_helpers import is_sonos
from syncsonic_ble.utils.logging_conf import get_logger
import subprocess
import time
from syncsonic_ble.utils.constants import (
    Msg, DBUS_OM_IFACE, DEVICE_INTERFACE,
    BLUEZ_SERVICE_NAME, A2DP_UUID,
)
from dbus import Interface
from syncsonic_ble.state_management.device_manager import DeviceManager
from syncsonic_ble.helpers.adapter_helpers import extract_mac, device_path_on_adapter
from syncsonic_ble.helpers.device_labels import register_device_label, format_device_label
from syncsonic_ble.state_change.action_planning import analyze_device

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Public intent enum + shared queue
# ---------------------------------------------------------------------------

class Intent(Enum):
    CONNECT_ONE  = auto()   # expects keys: mac, friendly_name, allowed
    DISCONNECT   = auto()   # expects key: mac
    SET_EXPECTED = auto()   # expects key: list[str]
    LOOPBACK_SYNC = auto()  # expects key: {"mac": <str>, "connected": <bool>}
    TEST_LATENCY = auto()   # New: test latency of connected speakers


work_q: Queue[Tuple[Intent, Dict]] = Queue()  # one global queue

# ---------------------------------------------------------------------------
# ConnectionService implementation
# ---------------------------------------------------------------------------

class ConnectionService:
    """Singleton‑style orchestrator (but caller holds the reference)."""

    # You almost certainly want exactly **one** instance – create at startup.

    def __init__(self):
        self.bus  = get_bus()          # singleton, thread‑safe
        self.scan = ScanManager()      # owns discovery

        self.bus.add_signal_receiver(
            self._on_props_changed,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged",
            path="/org/bluez",
        )

        self.expected: set[str] = set()
        self.loopbacks: set[str] = set()  # macs that already have loopbacks
        self._device_manager: Optional[DeviceManager] = None  # injected for Wi‑Fi status

        self.actuation = get_actuation_manager()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()
        self._char = None  # will be injected later
        self._refresh_transport_profile()
        logger.info("ConnectionService worker thread started")

    def set_device_manager(self, dev_mgr: DeviceManager) -> None:
        """Inject DeviceManager for Wi‑Fi connected tracking and status."""
        self._device_manager = dev_mgr

    # -----------------------------
    # Public helper: enqueue intents
    # -----------------------------

    def submit(self, intent: Intent, payload: Dict):
        """Called by *any* transport thread (Flask / BLE etc.)."""
        work_q.put((intent, payload))

    def _refresh_transport_profile(self) -> None:
        wifi_active = bool(self._device_manager and self._device_manager.wifi_connected)
        publish_transport_profile(wifi_session_active=wifi_active)

    def _ensure_output_actuation(self, mac: str, requested_delay_ms: Optional[float] = None) -> bool:
        target_delay = requested_delay_ms
        if target_delay is None:
            target_delay = self.actuation.get_commanded_delay(mac)
        else:
            target_delay = get_transport_base_ms() + max(0.0, float(target_delay))
        ok, _snapshot = self.actuation.apply_fallback_delay(mac, float(target_delay), mode="provision")
        if ok:
            self.loopbacks.add(mac.upper())
        return ok

    def _apply_requested_settings(
        self,
        mac: str,
        settings: Optional[Dict],
        *,
        apply_delay: bool = True,
    ) -> None:
        if not settings:
            return

        delay_ms = settings.get("latency")
        if apply_delay and delay_ms is not None:
            self._ensure_output_actuation(mac, requested_delay_ms=float(delay_ms))

        volume = settings.get("volume")
        if volume is None:
            return

        balance = float(settings.get("balance", 0.5))
        balance = max(0.0, min(1.0, balance))
        volume = int(volume)
        if balance >= 0.5:
            left = round(volume * (1 - balance) * 2)
            right = volume
        else:
            left = volume
            right = round(volume * balance * 2)
        left = min(max(left, 0), 150)
        right = min(max(right, 0), 150)
        publish_output_mix(mac, left_percent=left, right_percent=right)

    # ------------------------------------------------------------------
    #  BlueZ signal helpers
    #
    #  • _extract_mac(path)       – pull "AA:BB:CC:DD:EE:FF" out of a
    #    BlueZ object path like "…/dev_AA_BB_CC_DD_EE_FF".
    #
    #  • _on_props_changed(...)   – runs in the GLib thread whenever
    #    BlueZ fires PropertiesChanged.  If the signal toggles the
    #    Connected flag for one of our *expected* speakers, we enqueue a
    #    LOOPBACK_SYNC intent so the worker thread can create/remove the
    #    loopback safely and in order.
    # ------------------------------------------------------------------

    _extract_mac = staticmethod(extract_mac)

    def _on_props_changed(self, sender, obj_path, iface, signal, params):
        #logger.info(f"PROP signal {mac} Connected={connected}")

        # Params is a GLib Variant → unpack to tuple
        changed_iface, changed_dict, _invalidated = params

        # We only care about Device1 property changes
        if changed_iface != "org.bluez.Device1":
            return

        mac = self._extract_mac(obj_path)
        if not mac or "Connected" not in changed_dict:
            return
        if mac.upper() not in self.expected:
            return

        connected = bool(changed_dict["Connected"])
        work_q.put((
            Intent.LOOPBACK_SYNC,
            {"mac": mac.upper(), "connected": connected}
        ))


    # -----------------------------
    # Worker loop (runs in its own thread)
    # -----------------------------

    def _run_worker(self):  # noqa: C901 – complexity is okay for now
        while True:
            try:
                intent, payload = work_q.get(timeout=1)
            except Empty:
                continue
            

            if intent is Intent.CONNECT_ONE and is_sonos(payload.get("mac", "")):
                # Wi‑Fi (Sonos) connect: ensure stream, then play on Sonos
                device_id = payload["mac"]
                try:
                    from syncsonic_ble.helpers.audio_stream_service import (
                        get_audio_stream_service,
                        ensure_icecast_running,
                        get_stream_url,
                        get_pi_ip,
                    )
                    from syncsonic_ble.helpers.sonos_controller import connect as sonos_connect
                    stream_svc = get_audio_stream_service()
                    if not ensure_icecast_running():
                        if self._char:
                            self._char.send_notification(
                                Msg.CONNECTION_STATUS_UPDATE,
                                {"phase": "connect_failed", "device": device_id, "error": "Icecast not running"},
                            )
                        continue
                    if not stream_svc.start_stream():
                        if self._char:
                            self._char.send_notification(
                                Msg.CONNECTION_STATUS_UPDATE,
                                {"phase": "connect_failed", "device": device_id, "error": "Stream start failed"},
                            )
                        continue
                    pi_ip = get_pi_ip()
                    if not pi_ip:
                        if self._char:
                            self._char.send_notification(
                                Msg.CONNECTION_STATUS_UPDATE,
                                {"phase": "connect_failed", "device": device_id, "error": "Could not determine Pi IP"},
                            )
                        continue
                    stream_url = get_stream_url(pi_ip)
                    if sonos_connect(device_id, stream_url):
                        if self._device_manager:
                            self._device_manager.add_wifi_connected(device_id)
                        self._refresh_transport_profile()
                        if self._char:
                            self._char.send_notification(
                                Msg.CONNECTION_STATUS_UPDATE,
                                {"phase": "connect_success", "device": device_id},
                            )
                        logger.info("[Sonos] Connected %s", device_id)
                    else:
                        if self._char:
                            self._char.send_notification(
                                Msg.CONNECTION_STATUS_UPDATE,
                                {"phase": "connect_failed", "device": device_id},
                            )
                except Exception as e:
                    logger.exception("[Sonos] Connect failed for %s: %s", device_id, e)
                    if self._char:
                        self._char.send_notification(
                            Msg.FAILURE,
                            {"error": str(e), "device": device_id, "phase": "connect_failed"},
                        )
                continue

            elif intent is Intent.SET_EXPECTED:
                macs: List[str] = [m.upper() for m in payload["macs"]]
                replace: bool = payload.get("replace", False)
                if replace:
                    self.expected = set(macs)
                else:
                    self.expected.update(macs)
                logger.info(f"Expected set now {self.expected}")

            elif intent is Intent.CONNECT_ONE:
                mac   = payload["mac"].upper()
                friendly_name = str(payload.get("friendly_name") or "").strip()
                if friendly_name:
                    register_device_label(mac, friendly_name)
                allow = [m.upper() for m in payload["allowed"]]
                requested_settings = payload.get("settings") or {}

                self.expected.add(mac)          # so loopback sync recognises it



                # # Re‑evaluate object tree each time

                om = Interface(self.bus.get_object("org.bluez", "/"), DBUS_OM_IFACE)
                obj_mgr = om.GetManagedObjects()

                status, ctrl_mac, dc_list = connect_one_plan(mac, allow, obj_mgr)

                for dev_mac, adapter_mac in dc_list:
                    path = device_path_on_adapter(self.bus, adapter_mac, dev_mac)
                    disconnect_device_dbus(path, dev_mac, self.bus)
                
                if status == "already_connected":
                    # use ctrl_mac (the HCI) and mac (the device) instead
                    device_path = device_path_on_adapter(self.bus, ctrl_mac, mac)
                    dev_obj = self.bus.get_object(BLUEZ_SERVICE_NAME, device_path)
                    dev_iface = Interface(dev_obj, DEVICE_INTERFACE)

                    logger.info(f"→ [DEBUG] Asking BlueZ to connect A2DP on {device_path} for {format_device_label(mac)}")
                    try:
                        dev_iface.ConnectProfile(A2DP_UUID)
                        logger.info("→ [DEBUG] ConnectProfile(A2DP) succeeded")
                    except Exception as e:
                        logger.info(f"⚠️ ConnectProfile(A2DP) failed: {e}")
                                    

                     # signal connect success
                    if self._char:
                        self._char.send_notification(
                            Msg.CONNECTION_STATUS_UPDATE,
                            {"phase": "connect_success", "device": mac}
                        )

                    if mac not in self.loopbacks:
                        if self._ensure_output_actuation(mac, requested_settings.get("latency")):
                            logger.info(f"✅ Loopback created for already-connected {format_device_label(mac)}")
                    self._apply_requested_settings(mac, requested_settings, apply_delay=False)
                    # we're done; nothing else to do for this intent
                    continue

                if status == "needs_connection" and ctrl_mac:
                    self._try_reconnect(ctrl_mac, mac, requested_settings)

            elif intent is Intent.DISCONNECT:
                mac_or_id = payload["mac"]
                if is_sonos(mac_or_id):
                    device_id = mac_or_id
                    try:
                        from syncsonic_ble.helpers.sonos_controller import disconnect as sonos_disconnect
                        from syncsonic_ble.helpers.audio_stream_service import get_audio_stream_service
                        sonos_disconnect(device_id)
                        if self._device_manager:
                            self._device_manager.remove_wifi_connected(device_id)
                        self._refresh_transport_profile()
                        wifi_left = (self._device_manager.wifi_connected if self._device_manager else set())
                        if not wifi_left:
                            get_audio_stream_service().stop_stream()
                        logger.info("[Sonos] Disconnected %s", device_id)
                    except Exception as e:
                        logger.exception("[Sonos] Disconnect failed for %s: %s", device_id, e)
                else:
                    mac = mac_or_id.upper()
                    self._disconnect_everywhere(mac)

            elif intent is Intent.LOOPBACK_SYNC:
                mac        = payload["mac"]
                connected  = payload["connected"]

                if connected and mac not in self.loopbacks:
                    if self._ensure_output_actuation(mac):
                        logger.info(f"✅ Loopback autoprovisioned for {format_device_label(mac)}")
                elif not connected and mac in self.loopbacks:
                    self.actuation.remove_output(mac)
                    self.loopbacks.remove(mac)
                    logger.info(f"🗑️  Loopback removed after disconnect for {format_device_label(mac)}")

            elif intent is Intent.TEST_LATENCY:
                macs = payload["macs"]
                logger.info(f"Starting latency test for {len(macs)} speakers")
                
                # Use existing PulseAudio controls through the service
                for mac in macs:
                    sink = f"bluez_sink.{mac.replace(':', '_')}.a2dp_sink"
                    
                    # Test each speaker
                    logger.info(f"Testing speaker {mac}")
                    
                    # Use existing PulseAudio helpers
                    # This will work because it's running in the service context
                    subprocess.run(["pactl", "set-sink-volume", sink, "50%"])
                    time.sleep(0.5)
                    # Record timestamp
                    timestamp = time.time()
                    logger.info(f"Speaker {mac} timestamp: {timestamp}")
                    time.sleep(1.5)
                    
                logger.info("Latency test complete")


    # -----------------------------
    # Core helpers (same thread)
    # -----------------------------

    def _try_reconnect(self, adapter_mac: str, dev_mac: str, requested_settings: Optional[Dict] = None):
        logger.info(f"FSM: reconnect {format_device_label(dev_mac)} via {adapter_mac}")

        # NEW → ask the object tree what still needs doing
        state = analyze_device(self.bus, adapter_mac, dev_mac)

        device_path = device_path_on_adapter(self.bus, adapter_mac, dev_mac)
        max_retry = 3
        attempt   = 0
        # send update to frontend
        if self._char:
            self._char.send_notification(
                Msg.CONNECTION_STATUS_UPDATE,
                {"phase": "fsm_start", "device": dev_mac}
            )
        while attempt < max_retry:
            logger.info(f"  → [{attempt+1}/3] state={state}")
            if self._char:
                self._char.send_notification(
                    Msg.CONNECTION_STATUS_UPDATE,
                    {
                        "phase": "fsm_state",
                        "device": dev_mac,
                        "state": state,
                        "attempt": attempt
                    }
                )

            # --- state handlers ---
            if state == "run_discovery":
                # signal discovery start
                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "discovery_start", "device": dev_mac}
                    )
                try:
                    self.scan.ensure_discovery(adapter_mac)
                    path = self.scan.wait_for_device(adapter_mac, dev_mac, 20)
                finally:
                    self.scan.release_discovery(adapter_mac)

                if not path:
                    logger.info("discovery timeout")
                    # signal discovery failed
                    if self._char:
                        self._char.send_notification(
                            Msg.CONNECTION_STATUS_UPDATE,
                            {"phase": "discovery_timeout", "device": dev_mac}
                        )
                    break
                # signal discovery success
                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "discovery_complete", "device": dev_mac}
                    )
                device_path = path
                state = "pair"   

            elif state == "pair":
                # signal pairing start
                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "pairing_start", "device": dev_mac}
                    )
                if pair_device_dbus(device_path, bus=self.bus):
                    state = "trust"
                    # signal pairing success
                    if self._char:
                        self._char.send_notification(
                            Msg.CONNECTION_STATUS_UPDATE,
                            {"phase": "pairing_success", "device": dev_mac}
                        )
                else:
                    attempt += 1
                    remove_device_dbus(device_path, self.bus)
                    logger.info("    ⚠️ pairing failed, removed device and retrying")
                    state = "run_discovery"  # remove & retry
                    # signal pairing failure
                    if self._char:
                        self._char.send_notification(
                            Msg.CONNECTION_STATUS_UPDATE,
                            {"phase": "pairing_failed", "device": dev_mac, "attempt": attempt}
                        )

            elif state == "trust":
                trust_device_dbus(device_path, self.bus)
                state = "connect"
                # signal trusting start
                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "trusting", "device": dev_mac}
                    )

            elif state == "connect":
                # signal connect start
                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "connect_start", "device": dev_mac}
                    )
                logger.info(f"entering the if connect loop")
                if connect_device_dbus(device_path, self.bus):
                    device_path = device_path_on_adapter(self.bus, adapter_mac, dev_mac)
                    dev_obj = self.bus.get_object(BLUEZ_SERVICE_NAME, device_path)
                    dev_iface = Interface(dev_obj, DEVICE_INTERFACE)

                    # wait for MediaTransport1 to appear
                    logger.info(f"→ Waiting for MediaTransport1 for {format_device_label(dev_mac)} before ConnectProfile...")
                    if not self.wait_for_media_transport(dev_mac):
                        logger.warning(f"❌ MediaTransport1 never appeared for {format_device_label(dev_mac)} after Device.Connect()")
                        attempt += 1
                        remove_device_dbus(device_path, self.bus)
                        state = "run_discovery"
                        continue  # retry

                    # Add a small delay to allow BlueZ to stabilize
                    time.sleep(1)

                    # Now attempt A2DP ConnectProfile
                    logger.info(f"→ [DEBUG] Asking BlueZ to connect A2DP on {device_path} for {format_device_label(dev_mac)}")
                    try:
                        dev_iface.ConnectProfile(A2DP_UUID)
                        logger.info("→ [DEBUG] ConnectProfile(A2DP) succeeded")
                    except Exception as e:
                        error_str = str(e)
                        if "InProgress" in error_str:
                            logger.info(f"→ A2DP connection in progress, waiting...")
                            # Wait a bit longer for the connection to complete
                            time.sleep(2)
                            try:
                                dev_iface.ConnectProfile(A2DP_UUID)
                                logger.info("→ [DEBUG] ConnectProfile(A2DP) succeeded on retry")
                            except Exception as e2:
                                logger.error(f"❌ ConnectProfile failed on retry for {format_device_label(dev_mac)}: {e2}")
                                if self._char:
                                    self._char.send_notification(
                                        Msg.CONNECTION_STATUS_UPDATE,
                                        {"phase": "connect_profile_failed", "device": dev_mac}
                                    )
                                continue
                        else:
                            logger.error(f"❌ ConnectProfile failed for {format_device_label(dev_mac)}: {e}")
                            if self._char:
                                self._char.send_notification(
                                    Msg.CONNECTION_STATUS_UPDATE,
                                    {"phase": "connect_profile_failed", "device": dev_mac}
                                )
                            continue

                    # signal connect success
                    if self._char:
                        self._char.send_notification(
                            Msg.CONNECTION_STATUS_UPDATE,
                            {"phase": "connect_success", "device": dev_mac}
                        )
                    if self._ensure_output_actuation(dev_mac, (requested_settings or {}).get("latency")):
                        logger.info("    ✅ connected + loopback")
                        self._apply_requested_settings(dev_mac, requested_settings, apply_delay=False)
                        return
                    else:
                        logger.info("    ⚠️ connected but loopback creation failed")
                        if self._char:
                            self._char.send_notification(
                                Msg.ERROR,
                                {"phase": "loopback creation failed, click connect again", "device": dev_mac}
                            )
                        return
                    
                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "connect_failed", "device": dev_mac, "attempt": attempt}
                    )
                logger.info(f"removing the device and retrying")
                remove_device_dbus(device_path, self.bus)
                state = "run_discovery"
                attempt += 1

        logger.info(f"    ❌ failed to reconnect {format_device_label(dev_mac)}")

    def _disconnect_everywhere(self, mac: str):
        om = Interface(self.bus.get_object("org.bluez", "/"), DBUS_OM_IFACE)
        obj_mgr = om.GetManagedObjects()
        for path, ifaces in obj_mgr.items():
            dev = ifaces.get("org.bluez.Device1")
            if dev and dev.get("Address", "").upper() == mac and dev.get("Connected", False):
                disconnect_device_dbus(path, mac, self.bus)
        if mac in self.loopbacks:
            self.actuation.remove_output(mac)
            self.loopbacks.remove(mac)

    
    # helper – ensure MediaTransport exists before we create loopback ------------

    def wait_for_media_transport(self, mac: str, timeout: int = 5) -> bool:
        fmt = mac.replace(":", "_")
        om_iface = Interface(self.bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                objs = om_iface.GetManagedObjects()
                if any(
                    "org.bluez.MediaTransport1" in ifaces and fmt in path
                    for path, ifaces in objs.items()
                ):
                    return True
            except Exception as e:
                logger.warning(f"[wait_for_media_transport] Error checking MediaTransport1 for {mac}: {e}")
            time.sleep(0.5)

        return False



