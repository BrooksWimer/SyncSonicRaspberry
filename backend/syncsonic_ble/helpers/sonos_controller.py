"""Control Sonos speakers: connect (play stream), disconnect (stop), volume."""
from __future__ import annotations

import threading
from typing import Dict, Optional, Any

from syncsonic_ble.helpers.device_type_helpers import is_sonos
from syncsonic_ble.helpers.sonos_discovery import discover_sonos
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

# Cache: uid -> SoCo speaker instance (IP may change; re-discover refreshes)
_speaker_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def _uid_from_device_id(device_id: str) -> Optional[str]:
    """Extract Sonos UID from device_id (e.g. sonos:RINCON_... -> RINCON_...)."""
    if not is_sonos(device_id):
        return None
    prefix = "sonos:"
    if device_id.startswith(prefix):
        return device_id[len(prefix):].strip()
    return None


def _get_speaker_for_uid(uid: str, ip_hint: Optional[str] = None):
    """
    Return SoCo speaker for the given UID. Uses cache; if missing, discovers and caches.
    ip_hint is ignored for identity but could be used for direct SoCo(ip) in future.
    """
    try:
        import soco
    except ImportError:
        log.error("[Sonos] soco not installed")
        return None

    with _cache_lock:
        if uid in _speaker_cache:
            sp = _speaker_cache[uid]
            # Quick liveness check optional; SoCo may still have old IP
            return sp

    speakers = discover_sonos(timeout=5)
    for d in speakers:
        did = d.get("device_id", "")
        if did.startswith("sonos:") and did == f"sonos:{uid}":
            try:
                # SoCo discovery returns SoCo objects; re-use or get by IP
                ip = d.get("ip", "")
                if ip:
                    sp = soco.SoCo(ip)
                    with _cache_lock:
                        _speaker_cache[uid] = sp
                    return sp
            except Exception as e:
                log.warning("[Sonos] Failed to create SoCo for %s: %s", uid, e)

    # Fallback: discover returns list of SoCo instances in some versions
    try:
        raw = soco.discovery.discover(timeout=5)
        for sp in raw or []:
            if getattr(sp, "uid", None) == uid:
                with _cache_lock:
                    _speaker_cache[uid] = sp
                return sp
    except Exception as e:
        log.warning("[Sonos] Discovery fallback failed: %s", e)

    return None


def connect(device_id: str, stream_url: str) -> bool:
    """
    Start playing stream_url on the Sonos device. Uses play_uri(..., force_radio=True).
    Returns True on success, False on failure.
    """
    uid = _uid_from_device_id(device_id)
    if not uid:
        log.error("[Sonos] Invalid device_id for connect: %s", device_id)
        return False

    speaker = _get_speaker_for_uid(uid)
    if not speaker:
        log.error("[Sonos] Could not find speaker for %s", device_id)
        return False

    try:
        speaker.play_uri(stream_url, title="SyncSonic Live", force_radio=True)
        if getattr(speaker, "mute", None) is not None:
            speaker.mute = False
        log.info("[Sonos] Playing stream on %s (%s)", speaker.player_name, device_id)
        return True
    except Exception as e:
        # Sonos often returns UPnP 701 "Transition not available" when Play is sent
        # immediately after SetAVTransportURI. URI is already set; wait and retry Play.
        err_str = str(e)
        if "701" in err_str or "Transition not available" in err_str:
            log.info("[Sonos] Got 701 after play_uri, waiting 2s then retrying Play")
            time.sleep(2)
            try:
                speaker.play()
                if getattr(speaker, "mute", None) is not None:
                    speaker.mute = False
                log.info("[Sonos] Playing stream on %s (%s) after retry", speaker.player_name, device_id)
                return True
            except Exception as e2:
                log.exception("[Sonos] connect retry failed for %s: %s", device_id, e2)
                return False
        log.exception("[Sonos] connect failed for %s: %s", device_id, e)
        return False


def disconnect(device_id: str) -> bool:
    """Stop playback on the Sonos device. Returns True on success."""
    uid = _uid_from_device_id(device_id)
    if not uid:
        log.error("[Sonos] Invalid device_id for disconnect: %s", device_id)
        return False

    speaker = _get_speaker_for_uid(uid)
    if not speaker:
        log.warning("[Sonos] Could not find speaker for disconnect %s (may already be off)", device_id)
        return True  # idempotent

    try:
        speaker.stop()
        log.info("[Sonos] Stopped %s (%s)", speaker.player_name, device_id)
        return True
    except Exception as e:
        log.warning("[Sonos] disconnect stop failed for %s: %s", device_id, e)
        return False


def set_volume(device_id: str, volume_0_100: int) -> bool:
    """Set Sonos volume 0–100. Returns True on success."""
    uid = _uid_from_device_id(device_id)
    if not uid:
        log.error("[Sonos] Invalid device_id for volume: %s", device_id)
        return False

    speaker = _get_speaker_for_uid(uid)
    if not speaker:
        log.error("[Sonos] Could not find speaker for volume %s", device_id)
        return False

    vol = max(0, min(100, volume_0_100))
    try:
        speaker.volume = vol
        log.debug("[Sonos] Volume set to %s for %s", vol, device_id)
        return True
    except Exception as e:
        log.warning("[Sonos] set_volume failed for %s: %s", device_id, e)
        return False


def get_transport_info(device_id: str) -> Optional[Dict[str, Any]]:
    """Get current transport state for the Sonos device (e.g. PLAYING, STOPPED)."""
    uid = _uid_from_device_id(device_id)
    if not uid:
        return None
    speaker = _get_speaker_for_uid(uid)
    if not speaker:
        return None
    try:
        info = speaker.get_current_transport_info()
        return dict(info) if info else None
    except Exception as e:
        log.debug("[Sonos] get_transport_info failed: %s", e)
        return None
