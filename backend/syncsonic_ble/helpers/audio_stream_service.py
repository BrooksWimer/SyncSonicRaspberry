"""Icecast + FFmpeg producer that streams ``virtual_out.monitor`` to ``/live.mp3``.

Restored from the pre-foundation snapshot (``wip/full-diff-snapshot-2026-03-11``)
unchanged in shape; the upstream PulseAudio source name (``virtual_out.monitor``)
is the same null-sink monitor the Slice 2 elastic engine still uses, so this
service plugs into the current architecture without changes.

Lifecycle: start when the first Wi-Fi (Sonos) device connects; stop when the
last Wi-Fi device disconnects. Watchdog restarts FFmpeg with exponential
backoff if it exits unexpectedly.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Optional

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

PULSE_SOCKET = os.environ.get("PULSE_SERVER", "unix:/run/syncsonic/pulse/native")
ICECAST_PORT = 8000
ICECAST_MOUNT = "/live.mp3"
ICECAST_SOURCE_PASSWORD = "syncsonic"  # must match /etc/icecast2/icecast.xml


def ensure_icecast_running() -> bool:
    """Return True if icecast2 is active. If inactive, attempt to start it.

    On the deployed Pi icecast2 is enabled at boot, so the start branch is a
    fallback for fresh setups. Failures are logged but non-fatal so the
    caller can decide whether to abort the Wi-Fi connect.
    """
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "icecast2"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and "active" in (r.stdout or "").strip().lower():
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("[Stream] systemctl is-active check failed: %s", exc)

    try:
        log.info("[Stream] icecast2 not active; attempting start")
        subprocess.run(["systemctl", "start", "icecast2"], check=True, timeout=10)
        time.sleep(1.0)
        r = subprocess.run(
            ["systemctl", "is-active", "icecast2"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and "active" in (r.stdout or "").strip().lower()
    except subprocess.CalledProcessError as exc:
        log.error("[Stream] failed to start icecast2: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.exception("[Stream] error starting icecast2: %s", exc)
    return False


def get_pi_ip() -> Optional[str]:
    """Best-effort LAN IP detection; the Sonos needs an HTTP URL pointing at
    the Pi, not at 127.0.0.1."""
    try:
        r = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout:
            for p in r.stdout.strip().split():
                if p and not p.startswith("127."):
                    return p
    except Exception:  # noqa: BLE001
        pass
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0] or None
    except Exception:  # noqa: BLE001
        return None


def get_stream_url(pi_ip: str, port: int = ICECAST_PORT, mount: str = ICECAST_MOUNT) -> str:
    mount = mount if mount.startswith("/") else "/" + mount
    return f"http://{pi_ip}:{port}{mount}"


class AudioStreamService:
    """Owns the FFmpeg process that publishes ``virtual_out.monitor`` to
    Icecast. Singleton; created lazily by ``get_audio_stream_service``."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._restart_thread: Optional[threading.Thread] = None
        self._stop_restart = threading.Event()
        self._restart_delay = 1.0
        self._max_restart_delay = 60.0

    def start_stream(self) -> bool:
        if not ensure_icecast_running():
            log.error("[Stream] icecast not running; cannot start FFmpeg")
            return False

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                log.info("[Stream] FFmpeg already running")
                return True

        pulse = os.environ.get("PULSE_SERVER", PULSE_SOCKET)
        env = os.environ.copy()
        env["PULSE_SERVER"] = pulse
        icecast_url = (
            f"icecast://source:{ICECAST_SOURCE_PASSWORD}@127.0.0.1:"
            f"{ICECAST_PORT}{ICECAST_MOUNT}"
        )
        cmd = [
            "ffmpeg", "-re",
            "-f", "pulse", "-i", "virtual_out.monitor",
            "-ac", "2",
            "-codec:a", "libmp3lame", "-b:a", "192k",
            "-content_type", "audio/mpeg",
            "-f", "mp3",
            icecast_url,
        ]
        try:
            log.info("[Stream] starting FFmpeg -> 127.0.0.1:%d%s", ICECAST_PORT, ICECAST_MOUNT)
            proc = subprocess.Popen(
                cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            with self._lock:
                self._process = proc
            self._stop_restart.clear()
            self._restart_thread = threading.Thread(
                target=self._watch_and_restart, daemon=True,
            )
            self._restart_thread.start()
            return True
        except FileNotFoundError:
            log.error("[Stream] ffmpeg not found; install: apt install ffmpeg")
            return False
        except Exception as exc:  # noqa: BLE001
            log.exception("[Stream] failed to start FFmpeg: %s", exc)
            return False

    def _watch_and_restart(self) -> None:
        while not self._stop_restart.is_set():
            with self._lock:
                proc = self._process
            if proc is None:
                break
            try:
                _, err = proc.communicate(timeout=1)
                if err:
                    for line in err.decode("utf-8", errors="replace").strip().splitlines():
                        log.debug("[Stream] ffmpeg: %s", line)
            except subprocess.TimeoutExpired:
                if proc.poll() is not None:
                    break
                continue
            break

        with self._lock:
            self._process = None
        if self._stop_restart.is_set():
            return

        log.warning("[Stream] FFmpeg exited; restart in %.1fs", self._restart_delay)
        self._stop_restart.wait(timeout=self._restart_delay)
        if self._stop_restart.is_set():
            return
        self._restart_delay = min(self._max_restart_delay, self._restart_delay * 2)
        self.start_stream()

    def stop_stream(self) -> None:
        self._stop_restart.set()
        with self._lock:
            proc = self._process
            self._process = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception as exc:  # noqa: BLE001
                log.warning("[Stream] error stopping FFmpeg: %s", exc)
        self._restart_thread = None
        self._restart_delay = 1.0
        log.info("[Stream] FFmpeg stopped")

    def is_streaming(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None


_singleton: Optional[AudioStreamService] = None


def get_audio_stream_service() -> AudioStreamService:
    global _singleton
    if _singleton is None:
        _singleton = AudioStreamService()
    return _singleton
