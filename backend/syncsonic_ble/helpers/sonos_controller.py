"""Sonos control surface: connect (play stream), disconnect (stop), volume, mute.

Restored from the pre-foundation snapshot (``wip/full-diff-snapshot-2026-03-11``)
and extended with ``mute()`` so the Slice 4 calibration sequence can silence
Wi-Fi outputs during Bluetooth captures (Sonos echo would otherwise contaminate
the cross-correlation of the BT speaker under test).

All operations are best-effort and idempotent; failures are logged and the
caller continues. Sonos UPnP can take a moment to settle so the connect path
also handles the "701 Transition not available" race after ``play_uri``.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from syncsonic_ble.helpers.device_type_helpers import is_sonos
from syncsonic_ble.helpers.sonos_discovery import discover_sonos
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

_speaker_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def _uid_from_device_id(device_id: str) -> Optional[str]:
    if not is_sonos(device_id):
        return None
    return device_id[len("sonos:"):].strip() or None


def _get_speaker_for_uid(uid: str):
    """Return a SoCo speaker for *uid*, caching the instance for reuse."""
    try:
        import soco
    except ImportError:
        log.error("[Sonos] soco not installed")
        return None

    with _cache_lock:
        cached = _speaker_cache.get(uid)
    if cached is not None:
        return cached

    for d in discover_sonos(timeout=5):
        if d.get("device_id") == f"sonos:{uid}":
            ip = d.get("ip") or ""
            if ip:
                try:
                    sp = soco.SoCo(ip)
                    with _cache_lock:
                        _speaker_cache[uid] = sp
                    return sp
                except Exception as exc:  # noqa: BLE001
                    log.warning("[Sonos] SoCo(%s) failed: %s", ip, exc)

    try:
        for sp in soco.discovery.discover(timeout=5) or []:
            if getattr(sp, "uid", None) == uid:
                with _cache_lock:
                    _speaker_cache[uid] = sp
                return sp
    except Exception as exc:  # noqa: BLE001
        log.debug("[Sonos] discovery fallback failed: %s", exc)

    return None


def connect(device_id: str, stream_url: str) -> bool:
    """Tell the Sonos to start playing *stream_url* (force_radio=True)."""
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
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        # Sonos sometimes returns UPnP 701 if Play is sent immediately after
        # SetAVTransportURI; the URI is set, just retry Play after a beat.
        if "701" in msg or "Transition not available" in msg:
            time.sleep(2.0)
            try:
                speaker.play()
                if getattr(speaker, "mute", None) is not None:
                    speaker.mute = False
                log.info(
                    "[Sonos] Playing stream on %s (%s) after 701 retry",
                    speaker.player_name, device_id,
                )
                return True
            except Exception as exc2:  # noqa: BLE001
                log.exception("[Sonos] connect retry failed for %s: %s", device_id, exc2)
                return False
        log.exception("[Sonos] connect failed for %s: %s", device_id, exc)
        return False


def disconnect(device_id: str) -> bool:
    """Stop playback on the Sonos. Idempotent."""
    uid = _uid_from_device_id(device_id)
    if not uid:
        log.error("[Sonos] Invalid device_id for disconnect: %s", device_id)
        return False
    speaker = _get_speaker_for_uid(uid)
    if not speaker:
        # Already off / unreachable; treat as success.
        return True
    try:
        speaker.stop()
        log.info("[Sonos] Stopped %s (%s)", speaker.player_name, device_id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("[Sonos] disconnect stop failed for %s: %s", device_id, exc)
        return False


def set_volume(device_id: str, volume_0_100: int) -> bool:
    uid = _uid_from_device_id(device_id)
    if not uid:
        return False
    speaker = _get_speaker_for_uid(uid)
    if not speaker:
        return False
    vol = max(0, min(100, int(volume_0_100)))
    try:
        speaker.volume = vol
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("[Sonos] set_volume failed for %s: %s", device_id, exc)
        return False


def get_volume(device_id: str) -> Optional[int]:
    uid = _uid_from_device_id(device_id)
    if not uid:
        return None
    speaker = _get_speaker_for_uid(uid)
    if not speaker:
        return None
    try:
        return int(getattr(speaker, "volume"))
    except Exception as exc:  # noqa: BLE001
        log.debug("[Sonos] get_volume failed for %s: %s", device_id, exc)
        return None


def get_transport_info(device_id: str) -> Optional[Dict[str, Any]]:
    uid = _uid_from_device_id(device_id)
    if not uid:
        return None
    speaker = _get_speaker_for_uid(uid)
    if not speaker:
        return None
    try:
        info = speaker.get_current_transport_info()
        return dict(info) if info else None
    except Exception as exc:  # noqa: BLE001
        log.debug("[Sonos] get_transport_info failed: %s", exc)
        return None


def mute(device_id: str, muted: bool) -> bool:
    """Set the Sonos mute state. Used by Slice 4 calibration to silence Wi-Fi
    outputs during a BT speaker's mic-capture window so the Sonos's delayed
    echo does not pollute the cross-correlation. SoCo mute is fast (~150 ms)
    and preserves the queue; the speaker resumes its stream URL when unmuted.
    """
    uid = _uid_from_device_id(device_id)
    if not uid:
        return False
    speaker = _get_speaker_for_uid(uid)
    if not speaker:
        return False
    try:
        speaker.mute = bool(muted)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("[Sonos] mute(%s, %s) failed: %s", device_id, muted, exc)
        return False
