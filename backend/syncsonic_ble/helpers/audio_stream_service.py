"""Icecast + FFmpeg streaming from SyncSonic Pulse virtual_out.monitor to /live.mp3."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Optional

from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

# Defaults
PULSE_SOCKET = os.environ.get("PULSE_SERVER", "unix:/run/syncsonic/pulse/native")
ICECAST_PORT = 8000
ICECAST_MOUNT = "/live.mp3"
ICECAST_SOURCE_PASSWORD = "syncsonic"  # must match Icecast server config


def ensure_icecast_running() -> bool:
    """
    Check systemctl is-active icecast2; if not active, start it.
    Returns True if Icecast is running (or was started), False otherwise.
    """
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "icecast2"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and "active" in (r.stdout or "").strip().lower():
            log.info("[Stream] Icecast2 already active")
            return True
    except Exception as e:
        log.warning("[Stream] systemctl is-active check failed: %s", e)

    try:
        log.info("[Stream] Starting icecast2...")
        subprocess.run(["systemctl", "start", "icecast2"], check=True, timeout=10)
        time.sleep(1)
        r = subprocess.run(
            ["systemctl", "is-active", "icecast2"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and "active" in (r.stdout or "").strip().lower():
            log.info("[Stream] Icecast2 started")
            return True
    except subprocess.CalledProcessError as e:
        log.error("[Stream] Failed to start icecast2: %s", e)
    except Exception as e:
        log.exception("[Stream] Error starting icecast2: %s", e)
    return False


def get_stream_url(pi_ip: str, port: int = ICECAST_PORT, mount: str = ICECAST_MOUNT) -> str:
    """Return the HTTP URL for the live stream (e.g. http://192.168.1.10:8000/live.mp3)."""
    mount = mount if mount.startswith("/") else "/" + mount
    return f"http://{pi_ip}:{port}{mount}"


def get_pi_ip() -> Optional[str]:
    """
    Determine this machine's primary LAN IP (for stream URL).
    Tries hostname -I first, then socket connect to external address.
    """
    try:
        r = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode == 0 and r.stdout:
            parts = r.stdout.strip().split()
            for p in parts:
                if p and not p.startswith("127."):
                    return p
    except Exception:
        pass
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0] or None
    except Exception:
        return None


class AudioStreamService:
    """
    Manages the FFmpeg process that streams virtual_out.monitor to Icecast.
    Start only when at least one Wi‑Fi sink is connected; stop when none.
    Restarts with exponential backoff if the process exits.
    """

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._restart_thread: Optional[threading.Thread] = None
        self._stop_restart = threading.Event()
        self._restart_delay = 1.0
        self._max_restart_delay = 60.0

    def start_stream(self) -> bool:
        """
        Ensure Icecast is running, then start FFmpeg streaming to icecast.
        Uses PULSE_SERVER from env (e.g. unix:/run/syncsonic/pulse/native).
        Icecast source password is hardcoded as 'syncsonic' (must match server).
        """
        if not ensure_icecast_running():
            log.error("[Stream] Icecast not running – cannot start stream")
            return False

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                log.info("[Stream] FFmpeg already running")
                return True

        pulse = os.environ.get("PULSE_SERVER", PULSE_SOCKET)
        env = os.environ.copy()
        env["PULSE_SERVER"] = pulse

        # icecast URL: source:password@127.0.0.1:8000/live.mp3
        icecast_url = f"icecast://source:{ICECAST_SOURCE_PASSWORD}@127.0.0.1:{ICECAST_PORT}{ICECAST_MOUNT}"

        cmd = [
            "ffmpeg", "-re",
            "-f", "pulse",
            "-i", "virtual_out.monitor",
            "-ac", "2",
            "-codec:a", "libmp3lame",
            "-b:a", "192k",
            "-content_type", "audio/mpeg",
            "-f", "mp3",
            icecast_url,
        ]

        try:
            log.info("[Stream] Starting FFmpeg stream to %s", icecast_url.split("@")[1])
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            with self._lock:
                self._process = proc

            # Optional: start a thread to read stderr for logs (and restart on exit)
            self._stop_restart.clear()
            self._restart_thread = threading.Thread(
                target=self._watch_and_restart,
                daemon=True,
            )
            self._restart_thread.start()
            return True
        except FileNotFoundError:
            log.error("[Stream] ffmpeg not found – install ffmpeg (apt install ffmpeg)")
            return False
        except Exception as e:
            log.exception("[Stream] Failed to start FFmpeg: %s", e)
            return False

    def _watch_and_restart(self) -> None:
        """Background: wait for process exit, log stderr, optionally restart with backoff."""
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

        log.warning("[Stream] FFmpeg exited – restart in %.1fs", self._restart_delay)
        self._stop_restart.wait(timeout=self._restart_delay)
        if self._stop_restart.is_set():
            return
        self._restart_delay = min(
            self._max_restart_delay,
            self._restart_delay * 2,
        )
        self.start_stream()

    def stop_stream(self) -> None:
        """Terminate the FFmpeg process and stop any restart loop."""
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
            except Exception as e:
                log.warning("[Stream] Error stopping FFmpeg: %s", e)
        if self._restart_thread is not None:
            self._restart_thread = None
        self._restart_delay = 1.0
        log.info("[Stream] FFmpeg stream stopped")

    def is_streaming(self) -> bool:
        """True if FFmpeg process is running."""
        with self._lock:
            return self._process is not None and self._process.poll() is None


# Singleton for use by connection_manager
_audio_stream_service: Optional[AudioStreamService] = None


def get_audio_stream_service() -> AudioStreamService:
    global _audio_stream_service
    if _audio_stream_service is None:
        _audio_stream_service = AudioStreamService()
    return _audio_stream_service
