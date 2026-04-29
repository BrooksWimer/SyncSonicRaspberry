"""Slice 4.2: single-speaker auto-calibration as a background process.

Closes the Slice 4 measurement loop on a SINGLE speaker:

  1. Mute every OTHER speaker via the Slice 3 C-filter ``mute_to``
     socket, so the mic captures one speaker only and the Slice 4.1
     analyzer can attribute the lag unambiguously.
  2. Concurrently capture ``virtual_out.monitor`` (the engine output)
     and the Jieli mic for ``capture_duration_sec`` seconds. Both are
     written to ``CAPTURE_DIR`` (tmpfs by default).
  3. Unmute the other speakers immediately after the capture window.
     Total user-perceptible single-speaker-only audio is the capture
     duration plus a ~100 ms ramp on each side.
  4. Run the Slice 4.1 analyzer to estimate the lag in samples.
  5. Validate the estimate's confidence; reject low-confidence results
     so a bad calibration cannot push a wrong delay onto a working
     speaker.
  6. If accepted, compute the alignment correction
     ``correction_ms = target_total_ms - measured_lag_ms`` and apply
     it via the existing actuation manager (same path as
     ``handle_set_latency``).
  7. Emit progress + final result events through ``on_event``, which
     the BLE handler wires to ``Characteristic.send_notification``.
     The same callback is also used to push to the telemetry log via
     a ``calibration_result`` event for after-the-fact analysis.

The whole flow runs on a daemon thread spawned by
``calibrate_speaker_async``. The BLE handler returns immediately with
an ACK so the GLib mainloop is never blocked on the 6+ second capture.
"""

from __future__ import annotations

import json
import os
import socket as _socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from syncsonic_ble.helpers.actuation import get_actuation_manager
from syncsonic_ble.telemetry import EventType
from syncsonic_ble.telemetry.event_writer import emit
from syncsonic_ble.utils.logging_conf import get_logger

logger = get_logger(__name__)

CAPTURE_DIR = Path("/run/syncsonic/slice4_2_calibration")
SOCK_DIR = Path("/tmp/syncsonic-engine")
# Cross-process source of truth for the latest user-facing delay
# every speaker has been published with. The actuation manager keeps
# an in-memory copy too, but this JSON file is the only thing every
# process (the GLib service, the actuation daemon, and a CLI test
# invocation of this module) can agree on.
CONTROL_STATE_PATH = Path("/tmp/syncsonic_pipewire/control_state.json")
DEFAULT_DURATION_SEC = 6.0
DEFAULT_TARGET_TOTAL_MS = 500.0   # alignment target; safely above any plausible measured lag
SOCKET_TIMEOUT_SEC = 0.5
RAMP_MS = 50                        # mute_to ramp duration; matches Slice 3.2

# Confidence acceptance thresholds. confidence_secondary is the
# discriminating one (peak vs second-best lobe with a 50 ms guard);
# confidence_primary is a sanity check that the peak rises above the
# overall noise floor. Both must pass.
#
# Threshold rationale (tuned against Slice 4.1 + 4.2 real captures):
#   - The LAG values are consistent across captures within ~30 ms,
#     even when confidence varies wildly. Confidence reflects how
#     SHARP the peak is, not how WRONG the lag is.
#   - With dense music (broadband, sustained), confidence_secondary
#     reaches 3-9x. With sparse music (quiet passages, percussion-
#     only), it can drop to 1.0-1.5x while the lag stays accurate.
#   - confidence_primary >= 3.0 still requires the peak to be 3x the
#     window mean, ruling out true silence-vs-noise garbage.
#   - confidence_secondary >= 1.5 means the peak is 50% larger than
#     any echo/alternative-lag candidate; correct enough for music-
#     based calibration. Slice 4.3+ will add multi-capture agreement
#     as the next layer of robustness.
MIN_CONFIDENCE_PRIMARY = 3.0
MIN_CONFIDENCE_SECONDARY = 1.5

# Lag plausibility bounds. A measured lag below MIN_LAG_MS or above
# MAX_LAG_MS is rejected as "implausible measurement"; this catches
# the case where the analyzer found a weak peak inside the search
# window but the speaker itself was never producing audio.
MIN_PLAUSIBLE_LAG_MS = 30.0
MAX_PLAUSIBLE_LAG_MS = 700.0

# Alignment-target safety bounds. We refuse to push more than
# MAX_ADJUSTMENT_MS of single-step adjustment (positive or negative)
# in one calibration cycle; this is a guard against a wildly wrong
# measurement shoving a working speaker far out of sync.
MAX_ADJUSTMENT_MS = 400.0
# Hard clamp on the published user-facing delay value, mirrored from
# ActuationManager.MIN_DELAY_MS / MAX_DELAY_MS so we don't try to
# publish something the actuation chain will silently clamp anyway.
MIN_USER_DELAY_MS = 20.0
MAX_USER_DELAY_MS = 4000.0

EventCallback = Callable[[str, Dict[str, Any]], None]


def _resolve_mic_source() -> Optional[str]:
    """Look up the Jieli mic source via pactl. Same lookup as
    mic_capture.py uses."""
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and "alsa_input.usb-Jieli" in parts[1]:
            return parts[1]
    return None


def _socket_for_mac(mac: str) -> Path:
    """``F4:6A:DD:D4:F3:C8`` -> ``/tmp/syncsonic-engine/syncsonic-delay-f4_6a_dd_d4_f3_c8.sock``"""
    fname = "syncsonic-delay-" + mac.replace(":", "_").lower() + ".sock"
    return SOCK_DIR / fname


def _other_sockets(target_mac: str) -> List[Path]:
    """All filter sockets except the target speaker's."""
    target_sock = _socket_for_mac(target_mac)
    if not SOCK_DIR.exists():
        return []
    return [p for p in SOCK_DIR.glob("syncsonic-delay-*.sock") if p != target_sock]


def _send_filter_command(sock_path: Path, line: str) -> Optional[Dict[str, Any]]:
    """Send a single command to a C filter Unix socket. Returns the
    parsed JSON response on success, None on any failure."""
    if not sock_path.exists():
        return None
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(SOCKET_TIMEOUT_SEC)
            s.connect(str(sock_path))
            s.sendall((line + "\n").encode("ascii"))
            buf = b""
            while b"\n" not in buf and len(buf) < 4096:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except (OSError, _socket.timeout):
        return None
    try:
        return json.loads(buf.decode("ascii", errors="replace").strip().split("\n")[0])
    except (ValueError, json.JSONDecodeError):
        return None


def _read_published_delay_ms(mac: str) -> Optional[float]:
    """Read the latest published user-facing delay for ``mac`` from the
    control-plane JSON file. Returns None if the file is missing,
    malformed, or has no entry for the target MAC. The actuation
    daemon already tolerates concurrent reads + writes via atomic
    rename, so a partial file is unlikely; we still defend against it
    by treating any parse error as 'no entry'."""
    try:
        with open(CONTROL_STATE_PATH, "r") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    outputs = state.get("outputs", {}) if isinstance(state, dict) else {}
    entry = outputs.get(mac.upper())
    if not isinstance(entry, dict):
        return None
    val = entry.get("delay_ms")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _capture_pair(
    duration_sec: float,
    out_dir: Path,
    target_mac: str,
) -> Optional[Dict[str, Path]]:
    """Concurrently parecord ``virtual_out.monitor`` and the Jieli mic
    for ``duration_sec`` seconds. Returns dict of paths or None on
    failure. Both files are stereo/mono respectively, s16le, 48 kHz -
    the analyzer mixes-down stereo as needed."""
    mic_source = _resolve_mic_source()
    if not mic_source:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    safe_mac = target_mac.replace(":", "_").lower()
    ref_wav = out_dir / f"ref_{safe_mac}_{ts}.wav"
    mic_wav = out_dir / f"mic_{safe_mac}_{ts}.wav"

    # parecord uses the existing PipeWire-Pulse server. Both processes
    # start within a few ms of each other; the analyzer's search
    # window comfortably absorbs the start delta.
    common = [
        "parecord", "--file-format=wav", "--rate=48000",
        "--format=s16le", "--process-time-msec=20",
    ]
    ref_proc = subprocess.Popen(
        common + ["--device=virtual_out.monitor", "--channels=2", str(ref_wav)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    mic_proc = subprocess.Popen(
        common + [f"--device={mic_source}", "--channels=1", str(mic_wav)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        time.sleep(duration_sec)
    finally:
        for p in (ref_proc, mic_proc):
            try:
                p.send_signal(2)  # SIGINT, lets parecord finalise the WAV header
                p.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                p.kill()
            except OSError:
                pass

    if not ref_wav.exists() or not mic_wav.exists():
        return None
    if ref_wav.stat().st_size < 1024 or mic_wav.stat().st_size < 1024:
        # parecord may produce a tiny header-only WAV if the source
        # disappeared mid-capture.
        return None
    return {"ref": ref_wav, "mic": mic_wav}


def _calibrate_blocking(
    target_mac: str,
    target_total_ms: float,
    capture_duration_sec: float,
    on_event: EventCallback,
) -> None:
    """The actual calibration steps. Runs on a daemon thread so the
    GLib mainloop is never blocked. ``on_event`` is invoked at each
    phase boundary - the BLE handler wires it to send_notification
    and we additionally mirror to the telemetry events stream."""

    def _emit(phase: str, **fields: Any) -> None:
        payload = {"mac": target_mac, "phase": phase, **fields}
        try:
            on_event(phase, payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("calibrate on_event callback failed: %s", exc)
        # Mirror to telemetry. We use a generic CALIBRATION_RESULT
        # event type so the analyzer can replay every step.
        try:
            emit(EventType.CALIBRATION_RESULT, payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("calibrate telemetry emit failed: %s", exc)

    _emit("started", target_total_ms=target_total_ms,
          capture_duration_sec=capture_duration_sec)

    target_sock = _socket_for_mac(target_mac)
    if not target_sock.exists():
        _emit("failed", reason="target_filter_socket_not_found",
              socket=str(target_sock))
        return

    others = _other_sockets(target_mac)
    _emit("muting_others", n_others=len(others),
          others=[p.name for p in others])
    for sock in others:
        if not _send_filter_command(sock, f"mute_to 0 {RAMP_MS}"):
            logger.warning("mute_to failed for %s; calibration may pick up cross-speaker bleed", sock)

    # Tiny settle so the 50 ms ramp completes before we start capturing.
    time.sleep(0.15)

    _emit("capturing", duration_sec=capture_duration_sec)
    capture = _capture_pair(capture_duration_sec, CAPTURE_DIR, target_mac)

    # Always restore the mutes, even on capture failure - we MUST NOT
    # leave the system silent.
    for sock in others:
        _send_filter_command(sock, f"mute_to 1000 {RAMP_MS}")
    _emit("unmuting_others_done", n_restored=len(others))

    if capture is None:
        _emit("failed", reason="capture_failed")
        return

    _emit("analyzing", ref=str(capture["ref"]), mic=str(capture["mic"]))

    # Import the analyzer lazily so a calibration that never runs
    # doesn't pull scipy into syncsonic_ble's startup path.
    try:
        from measurement.analyze_lag import (  # type: ignore
            estimate_lag_samples, load_wav_mono,
        )
    except ImportError as exc:
        _emit("failed", reason="analyzer_import_failed", error=str(exc))
        return

    try:
        ref_arr, ref_sr = load_wav_mono(capture["ref"])
        mic_arr, mic_sr = load_wav_mono(capture["mic"])
    except Exception as exc:  # noqa: BLE001
        _emit("failed", reason="wav_load_failed", error=repr(exc))
        return

    if ref_sr != mic_sr:
        _emit("failed", reason="sample_rate_mismatch",
              ref_sr=ref_sr, mic_sr=mic_sr)
        return

    try:
        est = estimate_lag_samples(
            ref_arr, mic_arr, sample_rate=ref_sr,
            min_lag_ms=-50.0, max_lag_ms=MAX_PLAUSIBLE_LAG_MS,
        )
    except Exception as exc:  # noqa: BLE001
        _emit("failed", reason="analyzer_failed", error=repr(exc))
        return

    measurement = {
        "lag_ms": round(est.lag_ms, 2),
        "lag_samples": est.lag_samples,
        "peak_correlation": round(est.peak_correlation, 4),
        "confidence_primary": round(est.confidence_primary, 2),
        "confidence_secondary": round(est.confidence_secondary, 2),
        "sample_rate": est.sample_rate,
    }

    # Reject low-confidence or implausible measurements. We do this
    # BEFORE applying any correction so a bad calibration cannot push
    # a wrong delay onto a working speaker.
    rejection = None
    if est.confidence_primary < MIN_CONFIDENCE_PRIMARY:
        rejection = (
            f"confidence_primary {est.confidence_primary:.2f} below "
            f"threshold {MIN_CONFIDENCE_PRIMARY}"
        )
    elif est.confidence_secondary < MIN_CONFIDENCE_SECONDARY:
        rejection = (
            f"confidence_secondary {est.confidence_secondary:.2f} below "
            f"threshold {MIN_CONFIDENCE_SECONDARY}"
        )
    elif not (MIN_PLAUSIBLE_LAG_MS <= est.lag_ms <= MAX_PLAUSIBLE_LAG_MS):
        rejection = (
            f"lag_ms {est.lag_ms:.1f} outside plausible window "
            f"[{MIN_PLAUSIBLE_LAG_MS}, {MAX_PLAUSIBLE_LAG_MS}]"
        )

    if rejection is not None:
        _emit("failed", reason="rejected_measurement", detail=rejection,
              measurement=measurement)
        return

    # Compute the new user-facing delay. The cross-correlation analyzer
    # returned the TOTAL acoustic lag (BlueZ buffer + speaker DSP +
    # propagation + whatever filter delay was already applied). To
    # change that total to ``target_total_ms`` we shift the published
    # user-delay by (target - measured). Working in user-delay space
    # rather than filter-delay space lets us stay agnostic to the
    # actuation manager's TRANSPORT_BASE_MS offset; the manager
    # already does that subtraction internally.
    #
    # We read the current user-delay from the on-disk control plane,
    # not from the actuation manager's in-memory state, so this code
    # works correctly even when invoked from a CLI process distinct
    # from the running service.
    current_user_delay_ms = _read_published_delay_ms(target_mac)
    if current_user_delay_ms is None:
        # No publish yet for this speaker. The actuation manager will
        # ensure_output() to its default starting point (100 ms), so
        # treat that as our pre-image.
        current_user_delay_ms = 100.0

    adjustment_ms = target_total_ms - est.lag_ms
    if abs(adjustment_ms) > MAX_ADJUSTMENT_MS:
        _emit("failed", reason="adjustment_too_large",
              adjustment_ms=round(adjustment_ms, 2),
              max_adjustment_ms=MAX_ADJUSTMENT_MS,
              current_user_delay_ms=round(current_user_delay_ms, 2),
              measurement=measurement)
        return

    new_user_delay_ms = current_user_delay_ms + adjustment_ms
    new_user_delay_ms_clamped = max(
        MIN_USER_DELAY_MS, min(MAX_USER_DELAY_MS, new_user_delay_ms),
    )
    clamped = abs(new_user_delay_ms_clamped - new_user_delay_ms) > 1e-3

    _emit("applying",
          current_user_delay_ms=round(current_user_delay_ms, 2),
          adjustment_ms=round(adjustment_ms, 2),
          new_user_delay_ms=round(new_user_delay_ms_clamped, 2),
          clamped_to_bounds=clamped,
          target_total_ms=target_total_ms,
          measurement=measurement)

    # Manager is per-process; in CLI mode this is a fresh instance,
    # in the BLE-handler path it's the same singleton the rest of
    # the service uses. Either way, calling apply_control_target
    # with backend=pipewire-node publishes to the on-disk control
    # plane and the (separate) actuation daemon picks it up.
    manager = get_actuation_manager()
    ok, snapshot = manager.apply_control_target(
        target_mac, delay_ms=new_user_delay_ms_clamped,
        rate_ppm=0.0, mode="calibrated",
    )
    if not ok:
        _emit("failed", reason="actuation_failed",
              measurement=measurement,
              new_user_delay_ms=round(new_user_delay_ms_clamped, 2))
        return

    _emit("applied",
          current_user_delay_ms=round(current_user_delay_ms, 2),
          adjustment_ms=round(adjustment_ms, 2),
          new_user_delay_ms=round(new_user_delay_ms_clamped, 2),
          target_total_ms=target_total_ms,
          measurement=measurement,
          actuation=snapshot if isinstance(snapshot, dict) else {})


def calibrate_speaker_async(
    target_mac: str,
    on_event: EventCallback,
    target_total_ms: float = DEFAULT_TARGET_TOTAL_MS,
    capture_duration_sec: float = DEFAULT_DURATION_SEC,
) -> threading.Thread:
    """Spawn the calibration on a daemon thread and return immediately.

    ``on_event(phase, payload)`` is invoked from the worker thread at
    each phase boundary. The BLE handler wires this to
    ``Characteristic.send_notification(Msg.CALIBRATION_RESULT, payload)``
    so the mobile app sees live progress + the final result without
    blocking the GLib mainloop on the capture window.
    """
    t = threading.Thread(
        target=_calibrate_blocking,
        args=(target_mac, float(target_total_ms),
              float(capture_duration_sec), on_event),
        name=f"syncsonic-calibrate-{target_mac.replace(':', '_').lower()}",
        daemon=True,
    )
    t.start()
    return t


def _cli() -> int:
    """Diagnostic CLI: run a calibration end-to-end from the command
    line without going through the BLE wrapper. Useful for ground-
    truth validation on the Pi before exposing the BLE command to
    the app. Print every progress + result event to stdout as JSON.

    Must run with the same env vars the syncsonic.service runs with -
    in particular SYNCSONIC_ACTUATION_BACKEND=pipewire-node and
    XDG_RUNTIME_DIR=/run/syncsonic - or apply_control_target will
    fall back to the legacy pulseaudio-loopback backend instead of
    publishing through the C filter control plane.
    """
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="python -m measurement.calibrate_one")
    p.add_argument("--mac", required=True,
                   help="Speaker MAC, e.g. F4:6A:DD:D4:F3:C8")
    p.add_argument("--target-total-ms", type=float, default=DEFAULT_TARGET_TOTAL_MS,
                   help="alignment target in ms; correction = target - measured_lag")
    p.add_argument("--duration", type=float, default=DEFAULT_DURATION_SEC,
                   help="capture duration in seconds (default 6)")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="wall-clock budget for the whole calibration (default 30s)")
    args = p.parse_args()

    done = threading.Event()
    final_phase = {"phase": "timeout"}

    def _print_event(phase: str, payload: Dict[str, Any]) -> None:
        # Print as a single line of JSON for grep-friendly output.
        print(json.dumps(payload, default=str), flush=True)
        if phase in ("applied", "failed"):
            final_phase["phase"] = phase
            done.set()

    calibrate_speaker_async(
        args.mac, _print_event,
        target_total_ms=args.target_total_ms,
        capture_duration_sec=args.duration,
    )
    finished = done.wait(timeout=args.timeout)
    if not finished:
        print(json.dumps({"phase": "cli_timeout",
                          "after_seconds": args.timeout}), flush=True)
        return 2
    return 0 if final_phase["phase"] == "applied" else 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli())
