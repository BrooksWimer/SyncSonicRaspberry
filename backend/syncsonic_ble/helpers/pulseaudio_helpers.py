# utils/audio_server.py
import subprocess
import time
from typing import Dict, List, Optional

# First-party logging -------------------------------------------------------
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

# Phone-ingress module-loopback ids, keyed by upper MAC. The phone is an A2DP
# source from PipeWire's perspective (bluez_input.<mac>); a small loopback
# copies it into virtual_out so the per-speaker delay filters can fan it out.
# WirePlumber's autoconnect heuristic *also* tends to wire bluez_input directly
# to the default sink, so this loopback is a belt-and-suspenders guarantee
# rather than the only path - but it gives us an explicit, named module we own
# and can unload cleanly on phone disconnect.
_PHONE_INGRESS_MODULES: Dict[str, str] = {}

# --------------------------------------------------------------------------
#  Public helpers
# --------------------------------------------------------------------------

def _find_loopback_module_ids(actual_sink_name: str) -> List[str]:
    modules_output = subprocess.run(
        ["pactl", "list", "short", "modules"],
        capture_output=True,
        text=True,
    )
    module_ids: List[str] = []
    for line in modules_output.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and "module-loopback" in parts[1] and actual_sink_name in line:
            module_ids.append(parts[0])
    return module_ids

def remove_loopback_for_device(mac: str):
    """Unload every loopback that targets the *sink* of the given BT MAC."""
    sink_name = f"bluez_sink.{mac.replace(':', '_')}.a2dp_sink"
    log.info("🗑️  Removing loopback(s) for %s", sink_name)
    for module_id in _find_loopback_module_ids(sink_name):
        subprocess.run(["pactl", "unload-module", module_id], check=False)

def setup_audio_server() -> bool:
    """Ensure the configured Pulse-compatible server is reachable and prepare a virtual_out sink.

    Returns
    -------
    bool
        True if everything is ready, False otherwise.
    """
    try:

        # Step 1: Check if the Pulse-compatible server is currently running
        log.info("Checking if audio server is responsive...")
        info_result = subprocess.run(["pactl", "info"], capture_output=True, text=True)

        if info_result.returncode != 0 or "Server Name" not in info_result.stdout:
            log.error("Pulse-compatible audio server is not responding")

            for i in range(5):
                result = subprocess.run(["pactl", "info"], capture_output=True, text=True)
                if result.returncode == 0 and "Server Name" in result.stdout:
                    log.info("Audio server is responsive (after %d attempt(s))", i + 1)
                    break
                time.sleep(1)
            else:
                log.error("Failed to reach audio server; aborting audio initialization")
                return False

        # Step 2: Check whether the virtual sink already exists
        existing = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True)
        if "virtual_out" in existing.stdout:
            log.info("Sink 'virtual_out' already exists; skipping creation")
            return True

        # Step 3: Create the virtual sink.
        #
        # priority.driver/priority.session are baked into the load-module
        # invocation so virtual_out wins PipeWire's graph clock election against
        # any bluez_output node (PW default ~1010, our WirePlumber rule pins
        # bluez to 100). A single source of truth, set at sink-create time, so
        # there is no race against later WirePlumber re-application or against a
        # post-create pw-cli set-param hack.
        log.info("Creating virtual sink 'virtual_out'")
        result = subprocess.run([
            "pactl", "load-module", "module-null-sink",
            "sink_name=virtual_out",
            "sink_properties=device.description=virtual_out"
            " priority.driver=10000"
            " priority.session=10000",
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

        log.info("Audio server initialization complete; default sink is 'virtual_out'")
        return True

    except Exception as e:
        log.exception("Unhandled exception during audio server initialization: %s", e)
        return False


def setup_pulseaudio() -> bool:
    """Backward-compatible alias for older imports."""
    return setup_audio_server()


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


# --------------------------------------------------------------------------
#  Phone ingress (A2DP source -> virtual_out)
# --------------------------------------------------------------------------


def _find_bluez_input_source_for_mac(mac: str) -> Optional[str]:
    """Return the PipeWire/Pulse source name for phone audio if present.

    When the phone is the A2DP source and the Pi is the sink, PipeWire exposes
    a capture source named ``bluez_input.<MAC_with_underscores>.<idx>``.
    """
    token = mac.upper().replace(":", "_")
    result = subprocess.run(
        ["pactl", "list", "short", "sources"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        if not name.startswith("bluez_input."):
            continue
        if token in name.upper().replace("-", "_"):
            return name
    return None


def ensure_phone_ingress_loopback(mac: str, wait_seconds: float = 25.0) -> bool:
    """Route phone Bluetooth audio (bluez_input) into virtual_out.

    Polls for the bluez_input source for up to wait_seconds (it appears
    asynchronously after BlueZ negotiates A2DP), then loads a module-loopback
    that copies the source into virtual_out playback. The per-speaker fan-out
    graph reads from virtual_out's monitor, so this is the entry point for all
    phone-originated audio.

    Idempotent: any pre-existing loopback owned by us for the same MAC is
    unloaded first. The module id is tracked in _PHONE_INGRESS_MODULES so
    remove_phone_ingress_loopback can find it on disconnect.
    """
    mac_u = mac.upper()
    remove_phone_ingress_loopback(mac_u)
    deadline = time.monotonic() + wait_seconds
    source_name: Optional[str] = None
    while time.monotonic() < deadline:
        source_name = _find_bluez_input_source_for_mac(mac_u)
        if source_name:
            break
        time.sleep(0.25)

    if not source_name:
        log.warning(
            "No bluez_input source for %s yet — is the phone connected for audio (A2DP sink on Pi)?",
            mac_u,
        )
        return False

    result = subprocess.run(
        [
            "pactl",
            "load-module",
            "module-loopback",
            f"source={source_name}",
            "sink=virtual_out",
            "sink_dont_move=true",
            "latency_msec=80",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error(
            "Failed to load phone ingress loopback %s -> virtual_out: %s",
            source_name,
            (result.stderr or result.stdout or "").strip(),
        )
        return False

    mod_id = result.stdout.strip()
    if mod_id.isdigit():
        _PHONE_INGRESS_MODULES[mac_u] = mod_id
    log.info("Phone ingress active: %s -> virtual_out (module %s)", source_name, mod_id)
    return True


def remove_phone_ingress_loopback(mac: str) -> None:
    """Unload the phone ingress loopback for this MAC, if any."""
    mac_u = mac.upper()
    mod_id = _PHONE_INGRESS_MODULES.pop(mac_u, None)
    if mod_id:
        subprocess.run(
            ["pactl", "unload-module", mod_id],
            check=False,
            capture_output=True,
        )
        log.info("Removed phone ingress loopback module %s for %s", mod_id, mac_u)
        return
    # Fallback scan: an older daemon instance may have created a phone ingress
    # loopback before we started tracking module ids, or our cache may have
    # been cleared. Walk the loaded module list and unload anything that looks
    # like a bluez_input.<mac> -> virtual_out loopback.
    token = mac_u.replace(":", "_")
    modules_output = subprocess.run(
        ["pactl", "list", "short", "modules"],
        capture_output=True,
        text=True,
    )
    for line in modules_output.stdout.strip().splitlines():
        if "module-loopback" not in line:
            continue
        if "virtual_out" not in line:
            continue
        if f"bluez_input.{token}" in line.replace("-", "_") or token in line.upper():
            parts = line.split()
            if parts and parts[0].isdigit():
                subprocess.run(
                    ["pactl", "unload-module", parts[0]],
                    check=False,
                    capture_output=True,
                )
                log.info(
                    "Removed phone ingress loopback module %s (fallback scan)",
                    parts[0],
                )
