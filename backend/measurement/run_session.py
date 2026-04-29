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


def _find_active_events_file(root: Path) -> Optional[Path]:
    events_dir = root / "events"
    if not events_dir.exists():
        return None
    candidates = sorted(events_dir.glob("syncsonic-events-*.jsonl"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def _slice_events(events_file: Path, start_iso: str, end_iso: str, dest: Path) -> int:
    """Copy events in [start_iso, end_iso] (lexicographic on wall_iso) into dest."""
    n_kept = 0
    with open(events_file, "r", encoding="ascii") as fin, open(dest, "w", encoding="ascii") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            wall = obj.get("wall_iso", "")
            # ISO-8601 strings sort correctly lexicographically when same TZ.
            if start_iso <= wall <= end_iso:
                fout.write(line + "\n")
                n_kept += 1
    return n_kept


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

    # Slice events
    events_file = _find_active_events_file(root)
    n_events = 0
    if events_file is not None:
        try:
            n_events = _slice_events(events_file, start_wall, end_wall, bundle_dir / "events.jsonl")
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
        "events_source_file": str(events_file) if events_file else None,
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
