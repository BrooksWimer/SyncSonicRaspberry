"""Slice 1 session runner.

Plays a fixed reference audio clip through the SyncSonic audio path,
captures the resulting telemetry events + microphone audio, and writes
a session bundle on disk that ``measurement.report`` can later turn
into a single-page markdown report.

Usage
-----
    python -m measurement.run_session --name test1 [--duration 30]
                                       [--reference /path/to/reference.wav]

Conventions
-----------
- A session bundle lives at
  ``$SYNCSONIC_TELEMETRY_ROOT/sessions/<NAME>-<UTC-iso8601>/``
  containing:
    session.json  - metadata (name, start/end wall + monotonic, args)
    events.jsonl  - the slice of the live events jsonl in this window
    mic/          - copies of the rolling mic segments at session-end
                    (mtime preserved; the analyzer joins on wall time)
- A ``sessions/latest`` symlink always points at the most recent
  bundle for convenience (``cat sessions/latest/session.json``).

Reference signal
----------------
If --reference is omitted and no default reference exists yet at
$SYNCSONIC_TELEMETRY_ROOT/reference-pinknoise-30s.wav, the runner
generates one using sox: 30 s of stereo pink noise at 48 kHz, -10 dBFS.
Pink noise is the standard broadband test signal: it exercises the
SBC codec across the full audible spectrum and any audible dropout in
the room is easy for both human ears and the analyzer to detect.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add backend/ to sys.path so we can import syncsonic_ble.* when invoked
# directly via "python -m measurement.run_session" from /backend.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from syncsonic_ble.telemetry import telemetry_root  # noqa: E402

DEFAULT_DURATION_SEC = 30
DEFAULT_MIC_DIR = "/run/syncsonic/mic"
DEFAULT_REFERENCE_NAME = "reference-pinknoise-30s.wav"
DEFAULT_TEST_VOLUME_PERCENT = 50  # safety: cap speaker volume during the test


def _list_bluez_sink_names() -> list:
    """Return current bluez_output sink names (one per connected BT speaker)."""
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    sinks = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("bluez_output."):
            sinks.append(parts[1])
    return sinks


def _get_sink_volume_percents(sink: str) -> Optional[tuple]:
    """Return (left_pct, right_pct) for a sink, or None if unreadable."""
    try:
        result = subprocess.run(
            ["pactl", "list", "sinks"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    in_block = False
    for line in result.stdout.splitlines():
        if f"Name: {sink}" in line:
            in_block = True
            continue
        if in_block and line.startswith("\tName:"):
            in_block = False
        if in_block and "Volume:" in line and "%" in line:
            # "Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: ..."
            parts = line.split("%")
            if len(parts) >= 2:
                try:
                    left = int(parts[0].split("/")[-1].strip())
                    right = int(parts[1].split("/")[-1].strip())
                    return (left, right)
                except (ValueError, IndexError):
                    return None
    return None


def _set_sink_volume_percent(sink: str, percent: int) -> bool:
    try:
        result = subprocess.run(
            ["pactl", "set-sink-volume", sink, f"{percent}%", f"{percent}%"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0


def _utc_iso_now_ms() -> str:
    now = datetime.now(timezone.utc)
    base = now.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{now.microsecond // 1000:03d}Z"


def _safe_filename_iso() -> str:
    """ISO-8601 with colons replaced for filesystem-friendly names."""
    return _utc_iso_now_ms().replace(":", "-")


def _generate_reference(path: Path, duration: int) -> bool:
    """Generate a 48 kHz stereo pink noise reference at -10 dBFS via sox."""
    print(f"[run_session] generating reference signal at {path}")
    try:
        subprocess.run(
            [
                "sox", "-n",
                "-r", "48000",
                "-c", "2",
                "-b", "16",
                str(path),
                "synth", str(duration), "pinknoise",
                "vol", "-10", "dB",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError:
        print("[run_session] sox not installed; cannot generate reference", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as exc:
        print(f"[run_session] sox failed: {exc.stderr!r}", file=sys.stderr)
        return False
    return True


def _find_active_events_files(root: Path, since_unix: float = 0.0) -> list:
    """Return all events jsonl files whose mtime is >= since_unix.

    Each Python process under syncsonic.service has its own EventWriter
    (the syncsonic_ble.main service, the pipewire_actuation_daemon, and
    the measurement.mic_capture process all open separate files). For a
    session window we need to merge events from every file that was
    being written into during the window, not just the most recent.
    """
    events_dir = root / "events"
    if not events_dir.exists():
        return []
    out = []
    for p in events_dir.glob("syncsonic-events-*.jsonl"):
        try:
            if p.stat().st_mtime >= since_unix:
                out.append(p)
        except OSError:
            continue
    return out


def _slice_events_multi(
    events_files: list, start_iso: str, end_iso: str, dest: Path
) -> tuple:
    """Merge events from all files in [start_iso, end_iso], sorted by wall_iso.

    Returns (n_events_kept, list_of_source_files_used).
    """
    rows: list = []
    sources_used: list = []
    for ef in events_files:
        n_from_this = 0
        try:
            with open(ef, "r", encoding="ascii") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    wall = obj.get("wall_iso", "")
                    if start_iso <= wall <= end_iso:
                        rows.append((wall, line))
                        n_from_this += 1
        except OSError:
            continue
        if n_from_this > 0:
            sources_used.append({"path": str(ef), "events": n_from_this})

    # ISO-8601 with the same TZ sorts lexicographically by time.
    rows.sort(key=lambda r: r[0])
    with open(dest, "w", encoding="ascii") as fout:
        for _, line in rows:
            fout.write(line + "\n")
    return len(rows), sources_used


def _snapshot_mic(mic_dir: Path, dest_dir: Path) -> dict:
    """Copy all rolling-*.wav from mic_dir into dest_dir, preserving mtime."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    files: list = []
    if not mic_dir.exists():
        return {"mic_dir": str(mic_dir), "files": [], "note": "mic_dir does not exist"}
    for src in sorted(mic_dir.glob("rolling-*.wav")):
        try:
            dst = dest_dir / src.name
            shutil.copy2(src, dst)
            stat = dst.stat()
            files.append({
                "name": src.name,
                "size_bytes": stat.st_size,
                "mtime_unix": stat.st_mtime,
            })
        except OSError as exc:
            files.append({"name": src.name, "error": repr(exc)})
    files.sort(key=lambda f: f.get("mtime_unix", 0.0))
    return {"mic_dir": str(mic_dir), "files": files}


def _play_reference_async(reference: Path) -> subprocess.Popen:
    return subprocess.Popen(
        ["paplay", "--volume=65536", str(reference)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _update_latest_symlink(sessions_dir: Path, target_name: str) -> None:
    link = sessions_dir / "latest"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
    except OSError:
        return
    try:
        link.symlink_to(target_name)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="SyncSonic Slice 1 session runner")
    parser.add_argument("--name", required=True, help="Session name (used in bundle dir)")
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION_SEC,
                        help=f"Session duration in seconds (default {DEFAULT_DURATION_SEC})")
    parser.add_argument("--reference", default=None, help="Path to reference WAV (auto-generated if omitted)")
    parser.add_argument("--mic-dir", default=os.environ.get("SYNCSONIC_MIC_DIR", DEFAULT_MIC_DIR),
                        help="Directory holding the rolling mic segments")
    parser.add_argument("--no-play", action="store_true",
                        help="Skip playing the reference (just snapshot the live system)")
    parser.add_argument("--volume-percent", type=int, default=DEFAULT_TEST_VOLUME_PERCENT,
                        help="Speaker volume percent during the test (restored after). "
                             f"Default {DEFAULT_TEST_VOLUME_PERCENT}. Set to 0 to skip volume control.")
    args = parser.parse_args()

    root = telemetry_root()
    sessions_dir = root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Reference signal
    reference_path = Path(args.reference) if args.reference else (root / DEFAULT_REFERENCE_NAME)
    if not args.no_play and not reference_path.exists():
        if not _generate_reference(reference_path, args.duration):
            print("[run_session] no reference signal available; aborting", file=sys.stderr)
            return 2

    # Bundle dir
    bundle_name = f"{args.name}-{_safe_filename_iso()}"
    bundle_dir = sessions_dir / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # Volume safety: if the user asked us to manage volume, snapshot
    # current per-sink volumes so we can restore at end. Then drop all BT
    # sinks to the requested test volume (default 50%) so the pink-noise
    # reference does not blast a room.
    saved_volumes: dict = {}
    if args.volume_percent > 0 and not args.no_play:
        for sink in _list_bluez_sink_names():
            current = _get_sink_volume_percents(sink)
            if current is not None:
                saved_volumes[sink] = current
                _set_sink_volume_percent(sink, args.volume_percent)
                print(f"[run_session] {sink}: {current[0]}%/{current[1]}% -> {args.volume_percent}% (test)")

    start_wall = _utc_iso_now_ms()
    start_monotonic = time.monotonic_ns()
    print(f"[run_session] session '{args.name}' starting at {start_wall}, duration {args.duration}s")

    # Play reference (background) and wait
    play_proc: Optional[subprocess.Popen] = None
    if not args.no_play:
        try:
            play_proc = _play_reference_async(reference_path)
        except FileNotFoundError:
            print("[run_session] paplay not installed; aborting", file=sys.stderr)
            return 3

    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        print("[run_session] interrupted; capturing partial window", file=sys.stderr)

    end_wall = _utc_iso_now_ms()
    end_monotonic = time.monotonic_ns()

    if play_proc is not None and play_proc.poll() is None:
        try:
            play_proc.terminate()
            play_proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            play_proc.kill()

    # Restore original speaker volumes
    for sink, (left, right) in saved_volumes.items():
        # pactl set-sink-volume <sink> L% R%
        try:
            subprocess.run(
                ["pactl", "set-sink-volume", sink, f"{left}%", f"{right}%"],
                capture_output=True, text=True, timeout=2.0, check=False,
            )
            print(f"[run_session] {sink}: restored to {left}%/{right}%")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            print(f"[run_session] WARNING: failed to restore {sink} volume", file=sys.stderr)

    # Slice events: merge from ALL events files whose mtime overlaps the
    # session window. Each Python process (main, actuation_daemon,
    # mic_capture) has its own writer/file; we want the union.
    # Use the start_unix - 60s as the file-mtime cutoff so we don't miss
    # files that were last touched just before our window opened.
    start_unix = time.time() - args.duration  # close enough; mtime not micro-precise
    events_files = _find_active_events_files(root, since_unix=start_unix - 60.0)
    n_events = 0
    sources_used: list = []
    try:
        n_events, sources_used = _slice_events_multi(
            events_files, start_wall, end_wall, bundle_dir / "events.jsonl"
        )
    except OSError as exc:
        print(f"[run_session] events slice failed: {exc}", file=sys.stderr)

    # Snapshot mic
    mic_info = _snapshot_mic(Path(args.mic_dir), bundle_dir / "mic")

    # Write session metadata
    session_meta = {
        "name": args.name,
        "duration_sec": args.duration,
        "start_wall_iso": start_wall,
        "end_wall_iso": end_wall,
        "start_monotonic_ns": start_monotonic,
        "end_monotonic_ns": end_monotonic,
        "reference_wav": str(reference_path) if not args.no_play else None,
        "events_sources": sources_used,
        "events_kept": n_events,
        "mic_snapshot": mic_info,
        "bundle_dir": str(bundle_dir),
    }
    with open(bundle_dir / "session.json", "w", encoding="ascii") as fh:
        json.dump(session_meta, fh, sort_keys=True, indent=2)

    _update_latest_symlink(sessions_dir, bundle_name)

    print(f"[run_session] bundle written: {bundle_dir}")
    print(f"[run_session] events kept: {n_events}, mic files: {len(mic_info['files'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
