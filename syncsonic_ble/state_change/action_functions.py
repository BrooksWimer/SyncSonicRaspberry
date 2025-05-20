# bt_helpers deals with DBus-level convenience helpers for devices.  For
# generic adapter helpers we rely on the shared utilities in ``adapters`` so we
# don't duplicate logic.  This lets every module use the *same* implementation
# and keeps adapter path computations in one place.

from gi.repository import GLib
import time
import dbus

from syncsonic_ble.helpers.adapter_helpers import adapter_prefix_from_path  # unified helper
from syncsonic_ble.helpers.pulseaudio_helpers import remove_loopback_for_device

def connect_device_dbus(device_path: str, bus) -> bool:
    try:
        dev_obj = bus.get_object("org.bluez", device_path)
        device = dbus.Interface(dev_obj, "org.bluez.Device1")
        device.Connect()
        return True
    except Exception as e:
     
        return False


def trust_device_dbus(device_path: str, bus) -> bool:
    try:
        dev_obj = bus.get_object("org.bluez", device_path)
        device = dbus.Interface(dev_obj, "org.bluez.Device1")
        device.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
        return True
    except Exception as e:
    
        return False


def pair_device_dbus(device_path: str, bus) -> bool:
    try:
        time.sleep(1.5)
        dev_obj = bus.get_object("org.bluez", device_path)
        device = dbus.Interface(dev_obj, "org.bluez.Device1")
        device.Pair()
        return True
    except Exception as e:
        if "AlreadyExists" in str(e):
          
            return True  # treat as success
        else:
      
            return False


def remove_device_dbus(device_path: str, bus) -> bool:
    adapter_path = adapter_prefix_from_path(device_path)
    try:
        ad_obj = bus.get_object("org.bluez", adapter_path)
        adapter = dbus.Interface(ad_obj, "org.bluez.Adapter1")
        adapter.RemoveDevice(device_path)
        return True
    except Exception as e:
  
        return False



def disconnect_device_dbus(device_path: str, mac: str, bus) -> bool:
    """
    Disconnects the specified Bluetooth device using its full D-Bus path.
    """
    try:
        dev_obj = bus.get_object("org.bluez", device_path)
        device = dbus.Interface(dev_obj, "org.bluez.Device1")
        device.Disconnect()
        remove_loopback_for_device(mac)

        return True
    except Exception as e:
    
        return False
    

def disconnect_all_instances(mac: str, objects: dict, bus) -> bool:
    """
    Disconnects the given device from all controllers where it is currently connected.
    Uses the full D-Bus object tree instead of any global state.
    """
    mac = mac.upper()
    mac_fmt = mac.replace(":", "_")
    attempted = False

    for path, ifaces in objects.items():
        if "org.bluez.Device1" in ifaces:
            dev_obj = bus.get_object("org.bluez", path)
            device = dbus.Interface(dev_obj, "org.bluez.Device1")
            address = dev_obj.get("Address", "").upper()
            connected = dev_obj.get("Connected", False)
            if address == mac and connected:
                try:
                    device.Disconnect()
                    remove_loopback_for_device(mac)
             
                    attempted = True
                except Exception as e:
                    pass
                   


    return attempted
