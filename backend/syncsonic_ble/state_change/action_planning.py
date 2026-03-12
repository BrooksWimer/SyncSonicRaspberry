from syncsonic_ble.utils.logging_conf import get_logger
from syncsonic_ble.helpers.adapter_helpers import device_path_on_adapter
from syncsonic_ble.helpers.device_labels import format_device_label
import os
import dbus

logger = get_logger(__name__)

reserved_hci = (os.getenv("RESERVED_HCI") or "").strip()
reserved_mac = (os.getenv("RESERVED_ADAPTER_MAC") or "").strip().upper()
if not reserved_hci and not reserved_mac:
    raise RuntimeError("Either RESERVED_ADAPTER_MAC or RESERVED_HCI must be set")


def _is_reserved_adapter(path: str, adapter_addr: str) -> bool:
    if reserved_mac:
        return adapter_addr.upper() == reserved_mac
    return path.split("/")[-1] == reserved_hci


def connect_one_plan(target_mac: str, allowed_macs: list[str], objects: dict) -> tuple[str, str, list[tuple[str, str]]]:
    """
    Determines the appropriate connection plan for a given target device:
    - If already connected correctly, returns 'already_connected'.
    - If needs connection and a controller is available, returns 'needs_connection'.
    - If no suitable controllers are available, returns 'error'.
    """
    target_mac = target_mac.upper()
    allowed_macs = [mac.upper() for mac in allowed_macs]
    disconnect_list = []
    target_connected_on = []
    config_speaker_usage = {}
    used_controllers = set()
    adapters = {}

    # Build a map of adapter MACs to their object paths
    for path, ifaces in objects.items():
        if "org.bluez.Adapter1" in ifaces:
            addr = ifaces["org.bluez.Adapter1"].get("Address", "").upper()
            if _is_reserved_adapter(path, addr):
                continue  # Skip reserved phone adapter
            adapters[addr] = path

    logger.info("Planning connection for target: %s", format_device_label(target_mac))
    logger.info("Allowed MACs in config: %s", allowed_macs)

    # Analyze all devices
    for path, ifaces in objects.items():
        dev = ifaces.get("org.bluez.Device1")
        if not dev:
            continue
        dev_mac = dev.get("Address", "").upper()
        adapter_prefix = "/".join(path.split("/")[:4])

        ctrl_mac = None
        for mac, adapter_path in adapters.items():
            if adapter_prefix == adapter_path:
                ctrl_mac = mac
                break

        if not ctrl_mac:
            continue

        if dev.get("Connected", False):
            logger.info("Found connected device: %s on %s", format_device_label(dev_mac), ctrl_mac)

            if dev_mac in allowed_macs:
                config_speaker_usage.setdefault(dev_mac, []).append(ctrl_mac)

            if dev_mac == target_mac:
                target_connected_on.append(ctrl_mac)
                logger.info("Target %s already connected on %s", format_device_label(dev_mac), ctrl_mac)

            elif dev_mac not in allowed_macs:
                disconnect_list.append((dev_mac, ctrl_mac))
                logger.info(
                    "Out-of-config device %s -> marked for disconnection",
                    format_device_label(dev_mac),
                )

            elif dev_mac in allowed_macs:
                used_controllers.add(ctrl_mac)
                logger.info(
                    "Config speaker %s occupies controller %s",
                    format_device_label(dev_mac),
                    ctrl_mac,
                )

    logger.info("Target is currently connected on: %s", target_connected_on)
    logger.info("Disconnect list built: %s", disconnect_list)
    logger.info("Controllers in use by config devices: %s", used_controllers)

    # Handle multiple connections of target
    if len(target_connected_on) > 1:
        controller_to_keep = target_connected_on[0]
        for ctrl_mac in target_connected_on[1:]:
            disconnect_list.append((target_mac, ctrl_mac))
        logger.info(
            "Target connected on multiple controllers, keeping %s, disconnecting others",
            controller_to_keep,
        )
        return "already_connected", controller_to_keep, disconnect_list

    # Target connected once: ensure it's not sharing with another config speaker
    if len(target_connected_on) == 1:
        controller = target_connected_on[0]
        for mac, controllers in config_speaker_usage.items():
            if mac != target_mac and controller in controllers:
                disconnect_list.append((target_mac, controller))
                logger.info(
                    "Target %s shares controller %s with config speaker %s, reallocating",
                    target_mac,
                    controller,
                    mac,
                )

                # Try to find a free controller
                for new_ctrl_mac in adapters:
                    if new_ctrl_mac not in used_controllers and new_ctrl_mac != controller:
                        logger.info("Assigning free controller %s to target %s", new_ctrl_mac, target_mac)
                        return "needs_connection", new_ctrl_mac, disconnect_list

                # Fallback: free a duplicate
                for mac2, controllers2 in config_speaker_usage.items():
                    if len(controllers2) > 1:
                        ctrl_to_free = controllers2[1]
                        disconnect_list.append((mac2, ctrl_to_free))
                        logger.info("Freeing %s from %s to connect target %s", ctrl_to_free, mac2, target_mac)
                        return "needs_connection", ctrl_to_free, disconnect_list

                logger.info("No controller available after rebalance for target %s", target_mac)
                return "error", "", disconnect_list

        return "already_connected", controller, disconnect_list

    # Target is not currently connected anywhere
    for ctrl_mac in adapters:
        if ctrl_mac not in used_controllers:
            logger.info("Free controller %s found for target %s", ctrl_mac, target_mac)
            return "needs_connection", ctrl_mac, disconnect_list

    for mac, controllers in config_speaker_usage.items():
        if len(controllers) > 1:
            ctrl_to_free = controllers[1]
            disconnect_list.append((mac, ctrl_to_free))
            logger.info("Freeing controller %s from %s to connect target %s", ctrl_to_free, mac, target_mac)
            return "needs_connection", ctrl_to_free, disconnect_list

    logger.info("No available controller found for target %s", target_mac)
    return "error", "", disconnect_list


def analyze_device(bus, adapter_mac: str, dev_mac: str) -> str:
    om = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
    objects = om.GetManagedObjects()

    # Locate the Device1 dictionary for this mac on the chosen adapter
    dev_path = device_path_on_adapter(bus, adapter_mac, dev_mac)
    dev = objects.get(dev_path, {}).get("org.bluez.Device1", {})

    if not dev:
        return "run_discovery"

    paired = dev.get("Paired", False)
    trusted = dev.get("Trusted", False)
    connected = dev.get("Connected", False)
    uuids = dev.get("UUIDs", [])

    has_audio = any("110b" in u.lower() for u in uuids)  # A2DP/AVRCP

    if connected and has_audio:
        return "already_connected"
    if not paired:
        return "pair"
    if not trusted:
        return "trust"
    if paired and trusted and not connected:
        return "connect"
    return "run_discovery"

