"""Always-on mic capture process for the Slice 1 telemetry stream.

Continuously records the USB measurement microphone (Jieli UAC) at
48 kHz mono s16le into a 6-segment * 10-second rolling window stored on
tmpfs (``/run/syncsonic/mic/`` by default, overridable via
SYNCSONIC_MIC_DIR). The session runner copies the relevant slice into
the persistent session bundle when it needs it; this process never
touches the SD card itself, so the ~96 KB/sec write rate causes no
flash wear.

Process model
-------------
- Spawned by ``start_syncsonic.sh`` once the audio runtime is up.
- Polls for the mic source every 0.5 s for up to 60 s before giving up
  (the source is created lazily when something opens it; PipeWire often
  takes a few seconds after service start).
- Spawns a single ffmpeg process with the segment muxer doing the
  rotation; we just monitor it. If ffmpeg dies we restart it after a
  short backoff.
- Emits ``mic_segment_written`` heartbeat events to the main telemetry
  stream every time a new segment is observed, so a stale or stuck
  ffmpeg is visible to the analyzer.

Why ffmpeg and not pure parecord
--------------------------------
parecord has no built-in time limit or segment rotation; we would have
to manage timers and file rotation in Python. ffmpeg's segment muxer
gives us exactly the rotation pattern we want with sub-100 ms gaps and
zero Python timer logic. ffmpeg is already installed on the deployed
Pi (verified pre-write) so this introduces no new dependency.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Add backend/ to sys.path so we can import syncsonic_ble.* when invoked
# directly via "python -m measurement.mic_capture" from /backend.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from syncsonic_ble.telemetry import EventType  # noqa: E402
from syncsonic_ble.telemetry.event_writer import emit, get_event_writer  # noqa: E402

DEFAULT_MIC_DIR = "/run/syncsonic/mic"
SEGMENT_DURATION_SEC = 10
NUM_SEGMENTS = 6  # 60 s rolling window total
MIC_SOURCE_PREFIX = "alsa_input.usb-Jieli"
SOURCE_WAIT_TIMEOUT_SEC = 60.0
SOURCE_POLL_INTERVAL_SEC = 0.5
HEARTBEAT_POLL_INTERVAL_SEC = 2.0
RESTART_BACKOFF_SEC = 5.0


def _find_mic_source() -> Optional[str]:
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and MIC_SOURCE_PREFIX in parts[1]:
            return parts[1]
    return None


def _wait_for_source(timeout_sec: float) -> Optional[str]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        src = _find_mic_source()
        if src:
            return src
        time.sleep(SOURCE_POLL_INTERVAL_SEC)
    return None


def _start_ffmpeg(source: str, mic_dir: Path) -> subprocess.Popen:
    out_pattern = str(mic_dir / "rolling-%d.wav")
    cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-nostdin",
        "-f", "pulse",
        "-i", source,
        "-ar", "48000",
        "-ac", "1",
        "-acodec", "pcm_s16le",
        "-f", "segment",
        "-segment_time", str(SEGMENT_DURATION_SEC),
        "-segment_wrap", str(NUM_SEGMENTS),
        "-reset_timestamps", "1",
        "-y",
        out_pattern,
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _emit_heartbeat_if_new(mic_dir: Path, last_seen: dict) -> None:
    """Look at the rolling files and emit one event per newly-rotated segment."""
    try:
        wavs = list(mic_dir.glob("rolling-*.wav"))
    except OSError:
        return
    for wav in wavs:
        try:
            stat = wav.stat()
        except OSError:
            continue
        prev_mtime = last_seen.get(wav.name)
        if prev_mtime is None or stat.st_mtime > prev_mtime + 0.5:
            # Either we have not seen this segment, or it has been
            # rewritten (the segment_wrap path on rollover). Emit.
            try:
                idx = int(wav.stem.split("-")[-1])
            except ValueError:
                idx = -1
            emit(EventType.MIC_SEGMENT_WRITTEN, {
                "path": str(wav),
                "size_bytes": stat.st_size,
                "segment_idx": idx,
                "mtime_unix": stat.st_mtime,
            })
            last_seen[wav.name] = stat.st_mtime


def main() -> int:
    mic_dir = Path(os.environ.get("SYNCSONIC_MIC_DIR", DEFAULT_MIC_DIR))
    try:
        mic_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[mic_capture] cannot create mic dir {mic_dir}: {exc}", file=sys.stderr)
        return 1

    source = _wait_for_source(SOURCE_WAIT_TIMEOUT_SEC)
    if not source:
        print(
            f"[mic_capture] no PipeWire source matching '{MIC_SOURCE_PREFIX}' after "
            f"{SOURCE_WAIT_TIMEOUT_SEC}s; exiting",
            file=sys.stderr,
        )
        emit(EventType.MIC_SEGMENT_WRITTEN, {
            "phase": "fatal",
            "reason": "no_source",
            "prefix": MIC_SOURCE_PREFIX,
        })
        return 1

    emit(EventType.MIC_SEGMENT_WRITTEN, {
        "phase": "starting",
        "source": source,
        "mic_dir": str(mic_dir),
        "segment_duration_sec": SEGMENT_DURATION_SEC,
        "num_segments": NUM_SEGMENTS,
    })

    stop = {"flag": False}

    def _on_signal(_signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    last_seen: dict = {}
    proc: Optional[subprocess.Popen] = None
    while not stop["flag"]:
        if proc is None or proc.poll() is not None:
            if proc is not None:
                emit(EventType.MIC_SEGMENT_WRITTEN, {
                    "phase": "ffmpeg_exited",
                    "returncode": proc.returncode,
                })
                time.sleep(RESTART_BACKOFF_SEC)
                if stop["flag"]:
                    break
            try:
                proc = _start_ffmpeg(source, mic_dir)
                emit(EventType.MIC_SEGMENT_WRITTEN, {
                    "phase": "ffmpeg_started",
                    "pid": proc.pid,
                })
            except (FileNotFoundError, OSError) as exc:
                emit(EventType.MIC_SEGMENT_WRITTEN, {
                    "phase": "ffmpeg_spawn_failed",
                    "error": repr(exc),
                })
                time.sleep(RESTART_BACKOFF_SEC)
                continue

        time.sleep(HEARTBEAT_POLL_INTERVAL_SEC)
        _emit_heartbeat_if_new(mic_dir, last_seen)

    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
    emit(EventType.MIC_SEGMENT_WRITTEN, {"phase": "stopped"})
    get_event_writer().close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
