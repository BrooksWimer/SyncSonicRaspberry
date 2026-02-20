# utils/pulseaudio.py
import subprocess
import time
from typing import List, Optional

# First-party logging -------------------------------------------------------
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------
#  Public helpers
# --------------------------------------------------------------------------

def remove_loopback_for_device(mac: str):
    """Unload every loopback that targets the *sink* of the given BT MAC."""
    sink_name = f"bluez_sink.{mac.replace(':', '_')}.a2dp_sink"
    log.info("🗑️  Removing loopback(s) for %s", sink_name)
    subprocess.call(["pactl", "unload-module", f"module-loopback sink={sink_name}"])

def setup_pulseaudio() -> bool:
    """Ensure PulseAudio is running and prepare a virtual_out sink.

    Returns
    -------
    bool
        True if everything is ready, False otherwise.
    """
    try:

        # Step 1: Check if PulseAudio is currently running
        log.info("Checking if PulseAudio daemon is responsive...")
        info_result = subprocess.run(["pactl", "info"], capture_output=True, text=True)

        if info_result.returncode != 0 or "Server Name" not in info_result.stdout:
            log.warning("PulseAudio not responding, attempting to start it")

            # Start PulseAudio with no idle timeout
            subprocess.run(["pulseaudio", "--start", "--exit-idle-time=-1"], check=False)

            # Wait up to 5 seconds for PulseAudio to come up
            for i in range(5):
                result = subprocess.run(["pactl", "info"], capture_output=True, text=True)
                if result.returncode == 0 and "Server Name" in result.stdout:
                    log.info("PulseAudio started successfully (after %d attempt(s))", i + 1)
                    break
                time.sleep(1)
            else:
                log.error("Failed to start PulseAudio; aborting audio initialization")
                return False

        # Step 2: Check whether the virtual sink already exists
        existing = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True)
        if "virtual_out" in existing.stdout:
            log.info("Sink 'virtual_out' already exists; skipping creation")
            return True

        # Step 3: Create the virtual sink
        log.info("Creating virtual sink 'virtual_out'")
        result = subprocess.run([
            "pactl", "load-module", "module-null-sink",
            "sink_name=virtual_out",
            "sink_properties=device.description=virtual_out",
        ], capture_output=True, text=True)

        if result.returncode != 0:
            log.error("Failed to load virtual sink: %s", result.stderr.strip())
            return False

        module_id = result.stdout.strip()
        log.info("Loaded module-null-sink (id=%s)", module_id)

        # Step 4: Set the virtual sink as the default sink
        set_result = subprocess.run(["pactl", "set-default-sink", "virtual_out"], capture_output=True, text=True)
        if set_result.returncode != 0:
            log.error("Unable to set 'virtual_out' as default sink: %s", set_result.stderr.strip())
            return False

        log.info("PulseAudio initialization complete; default sink is 'virtual_out'")
        return True

    except Exception as e:
        log.exception("Unhandled exception during PulseAudio initialization: %s", e)
        return False


def create_loopback(expected_sink_prefix: str, latency_ms: int = 100, wait_seconds: int = 5) -> bool:
    """
    Waits for a specific sink to appear (matching by prefix), unloads any existing loopbacks for it,
    and then creates a clean new loopback.
    """
    def find_actual_sink_name() -> str:
        result = subprocess.run(["pactl", "list", "sinks", "short"],
                                capture_output=True, text=True)
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].startswith(expected_sink_prefix):
                return parts[1]
        return None

    def unload_conflicting_loopbacks(actual_sink_name: str):
        modules_output = subprocess.run(["pactl", "list", "short", "modules"],
                                        capture_output=True, text=True)
        for line in modules_output.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2 and "module-loopback" in parts[1] and actual_sink_name in line:
                module_id = parts[0]
                log.debug("↺ Unloading conflicting loopback module %s for %s", module_id, actual_sink_name)
                subprocess.run(["pactl", "unload-module", module_id])

    def load_loopback(actual_sink_name: str):
        """
        Load a loopback from virtual_out.monitor to a Bluetooth sink.
        Latency is controlled explicitly via latency_msec.
        """
        result = subprocess.run([
            "pactl", "load-module", "module-loopback",
            "source=virtual_out.monitor",
            f"sink={actual_sink_name}",
            "source_dont_move=true",
            f"latency_msec={latency_ms}",
        ], capture_output=True, text=True)

        return result

    log.info("🌀 Creating loopback: virtual_out.monitor → %s* (timeout %ss)", expected_sink_prefix, wait_seconds)
    for _ in range(wait_seconds * 2):
        actual_sink_name = find_actual_sink_name()
        if actual_sink_name:
            log.debug("Found matching sink %s", actual_sink_name)
            unload_conflicting_loopbacks(actual_sink_name)
            result = load_loopback(actual_sink_name)
            if result.returncode == 0:
                log.info("✅ Loopback established for %s", actual_sink_name)
                return True
            else:
                log.error("❌ Failed to load loopback module: %s", result.stderr.strip())
                return False
        time.sleep(0.5)

    log.error("⏰ Timeout – sink %s not found within %s seconds", expected_sink_prefix, wait_seconds)
    return False



