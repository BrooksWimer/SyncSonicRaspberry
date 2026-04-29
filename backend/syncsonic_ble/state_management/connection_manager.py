"""Thread-safe Bluetooth connection orchestrator for SyncSonic."""

from __future__ import annotations

import subprocess
import threading
import time
from enum import Enum, auto
from queue import Empty, Queue
from typing import Dict, List, Optional, Tuple

from dbus import Interface

from syncsonic_ble.helpers.actuation import get_actuation_manager
from syncsonic_ble.helpers.adapter_helpers import (
    device_path_on_adapter,
    extract_mac,
    is_device_on_reserved_adapter,
)
from syncsonic_ble.helpers.device_type_helpers import is_sonos
from syncsonic_ble.helpers.pipewire_control_plane import publish_output_mix
from syncsonic_ble.state_change.action_functions import (
    connect_device_dbus,
    disconnect_device_dbus,
    pair_device_dbus,
    remove_device_dbus,
    trust_device_dbus,
)
from syncsonic_ble.helpers.pulseaudio_helpers import (
    ensure_phone_ingress_loopback,
    remove_phone_ingress_loopback,
)
from syncsonic_ble.state_change.action_planning import analyze_device, connect_one_plan
from syncsonic_ble.state_management.bus_manager import get_bus
from syncsonic_ble.state_management.scan_manager import ScanManager
from syncsonic_ble.utils.constants import (
    A2DP_UUID,
    BLUEZ_SERVICE_NAME,
    DBUS_OM_IFACE,
    DEVICE_INTERFACE,
    Msg,
)
from syncsonic_ble.utils.logging_conf import get_logger

logger = get_logger(__name__)


class Intent(Enum):
    CONNECT_ONE = auto()
    DISCONNECT = auto()
    SET_EXPECTED = auto()
    LOOPBACK_SYNC = auto()
    TEST_LATENCY = auto()


work_q: Queue[Tuple[Intent, Dict]] = Queue()


class ConnectionService:
    """Singleton-style orchestrator for Bluetooth operations."""

    _extract_mac = staticmethod(extract_mac)

    def __init__(self):
        self.bus = get_bus()
        self.scan = ScanManager()

        self.bus.add_signal_receiver(
            self._on_props_changed,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged",
            path="/org/bluez",
        )

        self.expected: set[str] = set()
        self.loopbacks: set[str] = set()
        # Per-MAC last-auto-reconnect timestamp (CLOCK_MONOTONIC). Used to
        # debounce one-shot reconnect attempts so duplicate Connected=False
        # signals (the device_manager and connection_manager both subscribe
        # to BlueZ PropertiesChanged) cannot enqueue a CONNECT_ONE storm.
        self._last_auto_reconnect: Dict[str, float] = {}
        self.actuation = get_actuation_manager()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()
        self._char = None
        logger.info("ConnectionService worker thread started")

    def submit(self, intent: Intent, payload: Dict):
        work_q.put((intent, payload))

    def _ensure_output_actuation(self, mac: str, requested_delay_ms: Optional[float] = None) -> bool:
        # Defense in depth: the phone connects to the RESERVED_HCI adapter as an
        # A2DP source and must never be treated as a delay-controlled output. If
        # this guard ever fires it means an upstream caller (a BLE handler or a
        # LOOPBACK_SYNC for a reserved-adapter device) leaked a phone MAC into
        # the speaker control plane, which would cause the actuation daemon to
        # spin forever trying to find a non-existent bluez_output sink.
        if is_device_on_reserved_adapter(self.bus, mac):
            logger.warning(
                "Refusing actuation for reserved-adapter device %s (phone MAC, not a speaker output)",
                mac,
            )
            return False
        target_delay = requested_delay_ms
        if target_delay is None:
            target_delay = self.actuation.get_commanded_delay(mac)
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

        # Same guard as _ensure_output_actuation: reserved-adapter devices are
        # not outputs and any settings publish on their MAC is invalid.
        if is_device_on_reserved_adapter(self.bus, mac):
            logger.debug(
                "Skipping settings apply for reserved-adapter device %s (phone MAC)",
                mac,
            )
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

    def _on_props_changed(self, sender, obj_path, iface, signal, params):
        changed_iface, changed_dict, _invalidated = params
        if changed_iface != "org.bluez.Device1":
            return

        mac = self._extract_mac(obj_path)
        if not mac or "Connected" not in changed_dict:
            return
        if mac.upper() not in self.expected:
            return

        connected = bool(changed_dict["Connected"])
        work_q.put((Intent.LOOPBACK_SYNC, {"mac": mac.upper(), "connected": connected}))

    def _run_worker(self):  # noqa: C901
        while True:
            try:
                intent, payload = work_q.get(timeout=1)
            except Empty:
                continue

            if intent is Intent.CONNECT_ONE and is_sonos(payload.get("mac", "")):
                if self._char:
                    self._char.send_notification(
                        Msg.ERROR,
                        {
                            "error": "Wi-Fi speaker support is disabled on the neutral foundation branch.",
                            "feature_disabled": True,
                            "feature": "wifi_speakers",
                            "device": payload.get("mac"),
                        },
                    )
                continue

            if intent is Intent.SET_EXPECTED:
                macs: List[str] = [mac.upper() for mac in payload["macs"]]
                replace: bool = payload.get("replace", False)
                if replace:
                    self.expected = set(macs)
                else:
                    self.expected.update(macs)
                logger.info("Expected set now %s", self.expected)
                continue

            if intent is Intent.CONNECT_ONE:
                mac = payload["mac"].upper()
                allowed = [allowed_mac.upper() for allowed_mac in payload["allowed"]]
                requested_settings = payload.get("settings") or {}

                self.expected.add(mac)

                om = Interface(self.bus.get_object("org.bluez", "/"), DBUS_OM_IFACE)
                obj_mgr = om.GetManagedObjects()

                status, ctrl_mac, disconnect_list = connect_one_plan(mac, allowed, obj_mgr)

                for dev_mac, adapter_mac in disconnect_list:
                    path = device_path_on_adapter(self.bus, adapter_mac, dev_mac)
                    disconnect_device_dbus(path, dev_mac, self.bus)

                if status == "already_connected":
                    device_path = device_path_on_adapter(self.bus, ctrl_mac, mac)
                    dev_obj = self.bus.get_object(BLUEZ_SERVICE_NAME, device_path)
                    dev_iface = Interface(dev_obj, DEVICE_INTERFACE)

                    logger.info("[DEBUG] Asking BlueZ to connect A2DP on %s", device_path)
                    try:
                        dev_iface.ConnectProfile(A2DP_UUID)
                        logger.info("[DEBUG] ConnectProfile(A2DP) succeeded")
                    except Exception as exc:  # noqa: BLE001
                        logger.info("ConnectProfile(A2DP) failed: %s", exc)

                    if self._char:
                        self._char.send_notification(
                            Msg.CONNECTION_STATUS_UPDATE,
                            {"phase": "connect_success", "device": mac},
                        )

                    if mac not in self.loopbacks and self._ensure_output_actuation(
                        mac,
                        requested_settings.get("latency"),
                    ):
                        logger.info("Loopback created for already-connected %s", mac)
                    self._apply_requested_settings(mac, requested_settings, apply_delay=False)
                    continue

                if status == "needs_connection" and ctrl_mac:
                    self._try_reconnect(ctrl_mac, mac, requested_settings)
                continue

            if intent is Intent.DISCONNECT:
                mac_or_id = payload["mac"]
                if is_sonos(mac_or_id):
                    if self._char:
                        self._char.send_notification(
                            Msg.ERROR,
                            {
                                "error": "Wi-Fi speaker support is disabled on the neutral foundation branch.",
                                "feature_disabled": True,
                                "feature": "wifi_speakers",
                                "device": mac_or_id,
                            },
                        )
                    continue

                # User-initiated disconnect: remove the MAC from expected so the
                # LOOPBACK_SYNC auto-reconnect logic does not immediately requeue
                # a connect attempt. Without this, "user pressed disconnect" and
                # "speaker dropped out" would be indistinguishable to the
                # auto-reconnect path.
                self.expected.discard(mac_or_id.upper())
                self._disconnect_everywhere(mac_or_id.upper())
                continue

            if intent is Intent.LOOPBACK_SYNC:
                mac = payload["mac"]
                connected = payload["connected"]

                # Reserved-adapter device (the phone): A2DP source -> Pi sink.
                # Audio enters PipeWire as bluez_input and must be copied into
                # virtual_out via the phone-ingress loopback. This is *not* a
                # speaker output and must never reach _ensure_output_actuation
                # (the same bug that Slice 0 Fix A guards against from above).
                # The phone-ingress setup blocks for up to 25 s waiting for the
                # bluez_input source to appear, so it runs on its own thread to
                # keep the worker queue responsive.
                #
                # NOTE: `mac` and `phone_mac` are bound as default arguments so
                # the closure captures the VALUE at thread-creation time, not a
                # reference to the worker-loop-scoped name. The deployed
                # wip-branch version had a late-binding bug here that surfaced
                # in 00:04:34 Pi journal evidence ("Phone ingress not
                # established for F4:6A..." referring to the VIZIO speaker MAC
                # because the worker's `mac` had been reassigned to a later
                # LOOPBACK_SYNC event by the time the polling thread timed out).
                if is_device_on_reserved_adapter(self.bus, mac):
                    if connected:
                        phone_mac = mac

                        def _run_phone_ingress(phone_mac=phone_mac) -> None:
                            ok = ensure_phone_ingress_loopback(phone_mac)
                            if ok:
                                logger.info("Phone audio ingress ready for %s", phone_mac)
                            else:
                                logger.warning(
                                    "Phone ingress not established for %s — ensure phone audio "
                                    "(A2DP) is connected to this Pi",
                                    phone_mac,
                                )

                        threading.Thread(
                            target=_run_phone_ingress,
                            name=f"phone-ingress-{phone_mac}",
                            daemon=True,
                        ).start()
                    else:
                        remove_phone_ingress_loopback(mac)
                    continue

                if connected and mac not in self.loopbacks:
                    if self._ensure_output_actuation(mac):
                        logger.info("Loopback autoprovisioned for %s", mac)
                elif not connected and mac in self.loopbacks:
                    self.actuation.remove_output(mac)
                    self.loopbacks.remove(mac)
                    logger.info("Loopback removed after disconnect for %s", mac)

                # Auto-reconnect: an "expected" speaker that disconnects without
                # the user asking should attempt one CONNECT_ONE retry. The FSM
                # (analyze_device + _try_reconnect) does its own 3-attempt
                # internal retry on top of this single requeue; if it fails, the
                # user can still press connect again from the app. Reserved
                # adapter devices (the phone) are never auto-reconnected because
                # they aren't speakers.
                mac_u = mac.upper()
                if (
                    not connected
                    and mac_u in self.expected
                    and not is_device_on_reserved_adapter(self.bus, mac)
                ):
                    now = time.monotonic()
                    last = self._last_auto_reconnect.get(mac_u, 0.0)
                    if now - last >= 2.0:
                        self._last_auto_reconnect[mac_u] = now
                        logger.info(
                            "Speaker %s disconnected unexpectedly while expected; queuing one-shot reconnect",
                            mac,
                        )
                        work_q.put((Intent.CONNECT_ONE, {
                            "mac": mac,
                            "friendly_name": "",
                            "allowed": list(self.expected),
                            "settings": {},
                        }))
                continue

            if intent is Intent.TEST_LATENCY:
                macs = payload["macs"]
                logger.info("Starting latency test for %d speakers", len(macs))
                for mac in macs:
                    sink = f"bluez_sink.{mac.replace(':', '_')}.a2dp_sink"
                    logger.info("Testing speaker %s", mac)
                    subprocess.run(["pactl", "set-sink-volume", sink, "50%"], check=False)
                    time.sleep(0.5)
                    logger.info("Speaker %s timestamp: %s", mac, time.time())
                    time.sleep(1.5)
                logger.info("Latency test complete")

    def _try_reconnect(
        self,
        adapter_mac: str,
        dev_mac: str,
        requested_settings: Optional[Dict] = None,
    ) -> None:
        logger.info("FSM: reconnect %s via %s", dev_mac, adapter_mac)

        state = analyze_device(self.bus, adapter_mac, dev_mac)
        device_path = device_path_on_adapter(self.bus, adapter_mac, dev_mac)
        max_retry = 3
        attempt = 0

        if self._char:
            self._char.send_notification(
                Msg.CONNECTION_STATUS_UPDATE,
                {"phase": "fsm_start", "device": dev_mac},
            )

        while attempt < max_retry:
            logger.info("  -> [%s/3] state=%s", attempt + 1, state)
            if self._char:
                self._char.send_notification(
                    Msg.CONNECTION_STATUS_UPDATE,
                    {
                        "phase": "fsm_state",
                        "device": dev_mac,
                        "state": state,
                        "attempt": attempt,
                    },
                )

            if state == "run_discovery":
                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "discovery_start", "device": dev_mac},
                    )
                try:
                    self.scan.ensure_discovery(adapter_mac)
                    path = self.scan.wait_for_device(adapter_mac, dev_mac, 20)
                finally:
                    self.scan.release_discovery(adapter_mac)

                if not path:
                    if self._char:
                        self._char.send_notification(
                            Msg.CONNECTION_STATUS_UPDATE,
                            {"phase": "discovery_timeout", "device": dev_mac},
                        )
                    break

                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "discovery_complete", "device": dev_mac},
                    )
                device_path = path
                state = "pair"
                continue

            if state == "pair":
                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "pairing_start", "device": dev_mac},
                    )
                if pair_device_dbus(device_path, bus=self.bus):
                    state = "trust"
                    if self._char:
                        self._char.send_notification(
                            Msg.CONNECTION_STATUS_UPDATE,
                            {"phase": "pairing_success", "device": dev_mac},
                        )
                    continue

                attempt += 1
                remove_device_dbus(device_path, self.bus)
                logger.info("Pairing failed, removed device and retrying")
                state = "run_discovery"
                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "pairing_failed", "device": dev_mac, "attempt": attempt},
                    )
                continue

            if state == "trust":
                trust_device_dbus(device_path, self.bus)
                state = "connect"
                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "trusting", "device": dev_mac},
                    )
                continue

            if state == "connect":
                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "connect_start", "device": dev_mac},
                    )
                if connect_device_dbus(device_path, self.bus):
                    device_path = device_path_on_adapter(self.bus, adapter_mac, dev_mac)
                    dev_obj = self.bus.get_object(BLUEZ_SERVICE_NAME, device_path)
                    dev_iface = Interface(dev_obj, DEVICE_INTERFACE)

                    logger.info("Waiting for MediaTransport1 for %s before ConnectProfile", dev_mac)
                    if not self.wait_for_media_transport(dev_mac):
                        logger.warning("MediaTransport1 never appeared for %s", dev_mac)
                        attempt += 1
                        remove_device_dbus(device_path, self.bus)
                        state = "run_discovery"
                        continue

                    time.sleep(1)

                    logger.info("[DEBUG] Asking BlueZ to connect A2DP on %s", device_path)
                    try:
                        dev_iface.ConnectProfile(A2DP_UUID)
                        logger.info("[DEBUG] ConnectProfile(A2DP) succeeded")
                    except Exception as exc:  # noqa: BLE001
                        error_str = str(exc)
                        if "InProgress" in error_str:
                            logger.info("A2DP connection in progress, waiting")
                            time.sleep(2)
                            try:
                                dev_iface.ConnectProfile(A2DP_UUID)
                                logger.info("[DEBUG] ConnectProfile(A2DP) succeeded on retry")
                            except Exception as retry_exc:  # noqa: BLE001
                                logger.error("ConnectProfile failed on retry for %s: %s", dev_mac, retry_exc)
                                if self._char:
                                    self._char.send_notification(
                                        Msg.CONNECTION_STATUS_UPDATE,
                                        {"phase": "connect_profile_failed", "device": dev_mac},
                                    )
                                continue
                        else:
                            logger.error("ConnectProfile failed for %s: %s", dev_mac, exc)
                            if self._char:
                                self._char.send_notification(
                                    Msg.CONNECTION_STATUS_UPDATE,
                                    {"phase": "connect_profile_failed", "device": dev_mac},
                                )
                            continue

                    if self._char:
                        self._char.send_notification(
                            Msg.CONNECTION_STATUS_UPDATE,
                            {"phase": "connect_success", "device": dev_mac},
                        )
                    if self._ensure_output_actuation(dev_mac, (requested_settings or {}).get("latency")):
                        logger.info("Connected + loopback")
                        self._apply_requested_settings(dev_mac, requested_settings, apply_delay=False)
                        return

                    logger.info("Connected but loopback creation failed")
                    if self._char:
                        self._char.send_notification(
                            Msg.ERROR,
                            {"phase": "loopback creation failed, click connect again", "device": dev_mac},
                        )
                    return

                if self._char:
                    self._char.send_notification(
                        Msg.CONNECTION_STATUS_UPDATE,
                        {"phase": "connect_failed", "device": dev_mac, "attempt": attempt},
                    )
                remove_device_dbus(device_path, self.bus)
                state = "run_discovery"
                attempt += 1
                continue

        logger.info("Failed to reconnect %s", dev_mac)

    def _disconnect_everywhere(self, mac: str) -> None:
        om = Interface(self.bus.get_object("org.bluez", "/"), DBUS_OM_IFACE)
        obj_mgr = om.GetManagedObjects()
        for path, interfaces in obj_mgr.items():
            device = interfaces.get("org.bluez.Device1")
            if device and device.get("Address", "").upper() == mac and device.get("Connected", False):
                disconnect_device_dbus(path, mac, self.bus)
        if mac in self.loopbacks:
            self.actuation.remove_output(mac)
            self.loopbacks.remove(mac)

    def wait_for_media_transport(self, mac: str, timeout: int = 5) -> bool:
        fmt = mac.replace(":", "_")
        om_iface = Interface(self.bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                objs = om_iface.GetManagedObjects()
                if any(
                    "org.bluez.MediaTransport1" in interfaces and fmt in path
                    for path, interfaces in objs.items()
                ):
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("[wait_for_media_transport] Error checking MediaTransport1 for %s: %s", mac, exc)
            time.sleep(0.5)

        return False
