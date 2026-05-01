"""Slice 4.2: single-speaker auto-calibration as a background process.

Two reference modes:

- **music** (default): correlate against whatever is already playing on
  ``virtual_out`` (typical Spotify/A2DP ingress).
- **startup_tune**: synthesise or reuse a short band-limited chirp WAV,
  play it into ``virtual_out`` via ``paplay`` during the capture window,
  and correlate against that deterministic stimulus — faster and sharper
  peaks when phone audio is paused so the reference tap is not dominated
  by unrelated music.

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
import shutil
import socket as _socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

from syncsonic_ble.helpers.actuation import ActuationManager, get_actuation_manager
from syncsonic_ble.telemetry import EventType
from syncsonic_ble.telemetry.event_writer import emit
from syncsonic_ble.utils.logging_conf import get_logger

logger = get_logger(__name__)

CAPTURE_DIR = Path("/run/syncsonic/slice4_2_calibration")
SOCK_DIR = Path("/tmp/syncsonic-engine")

CalibrationMode = Literal["music", "startup_tune"]
CALIBRATION_MODE_MUSIC: CalibrationMode = "music"
CALIBRATION_MODE_STARTUP_TUNE: CalibrationMode = "startup_tune"
# Floor for the per-speaker capture duration. We never run shorter than
# this even at filter=0 so the analyzer always has plenty of post-signal
# tail to compute confidence against, and the cross-correlation has a
# clean noise floor reference. The chirp itself is only 2.65 s, so 10 s
# leaves ~7 s of post-signal room data to define the noise floor — that
# is what lets us trust the loud direct-path peak even when the speaker
# is a multi-driver soundbar with non-trivial reflections.
DEFAULT_DURATION_STARTUP_SEC = 10.0
# Music-mode floor. Same idea: a continuous 10 s window gives the
# correlator plenty of overlap region at any plausible BT inherent lag.
DEFAULT_DURATION_SEC = 10.0

# Generous upper bound on the inherent BT chain (codec + radio + speaker
# DSP + air). Used only for sizing the *post-signal tail*; the search
# window does NOT centre on this value (we deliberately give the analyzer
# room to find the loud chirp wherever it lands within a wide window
# anchored on the speaker's current filter delay).
BT_INHERENT_MAX_MS = 800.0
# Tail margin after the chirp/music finishes arriving at the mic.
# Generous on purpose: extra post-signal silence lets the cross-correlator
# compute a stable noise-floor reference and gives reflections from
# multi-driver speakers (soundbars in particular) time to die out
# before they could contaminate a future capture.
CAPTURE_TAIL_MARGIN_SEC = 4.0
# paplay in chirp mode takes ~80 ms to begin emitting after we start
# the parecord processes; round up generously.
PAPLAY_STARTUP_OVERHEAD_SEC = 0.20
# Music mode wants at least this many seconds of overlap region for
# the cross-correlator to average over. Generous (5 s) so the result
# is stable even during quiet musical passages.
MUSIC_OVERLAP_MIN_SEC = 5.0
# Cross-process source of truth for the latest user-facing delay
# every speaker has been published with. The actuation manager keeps
# an in-memory copy too, but this JSON file is the only thing every
# process (the GLib service, the actuation daemon, and a CLI test
# invocation of this module) can agree on.
CONTROL_STATE_PATH = Path("/tmp/syncsonic_pipewire/control_state.json")
DEFAULT_TARGET_TOTAL_MS = 500.0   # alignment target; safely above any plausible measured lag
SOCKET_TIMEOUT_SEC = 0.5
RAMP_MS = 50                        # mute_to ramp duration; matches Slice 3.2
# After sending mute_to to the other BT speakers, wait this long before
# starting the capture. The C-filter gain ramp finishes in RAMP_MS (50 ms),
# but the downstream A2DP transmit buffer still holds up to ~800 ms of audio
# that was already committed before the gain reached 0. Without a settle wait
# the first ~800 ms of the capture window includes fading bleed from the
# just-muted speakers, which creates spurious correlation energy (especially
# in music mode where all speakers are playing the same content).
MUTE_SETTLE_SEC = 1.0

# Confidence acceptance thresholds. confidence_secondary is the
# discriminating one (peak vs second-best lobe with a 50 ms guard);
# confidence_primary is a sanity check that the peak rises above the
# overall noise floor. Both must pass.
#
# Threshold rationale (tuned against real captures across BT speakers
# AND multi-driver soundbars at high filter delays):
#   - LAG estimates are consistent across captures within ~30 ms,
#     even when confidence varies. Confidence reflects how SHARP the
#     peak is, not how WRONG the lag is.
#   - confidence_primary >= 3.0 keeps out true silence-vs-noise garbage.
#   - confidence_secondary >= 1.2 (loosened from 1.5) accepts measurements
#     where a soundbar's room reflection sits within ~20 % of the direct
#     path's correlation amplitude. Combined with the wider capture
#     window (10 s minimum, 4 s post-signal tail) and the loud chirp
#     (target_peak 0.40), this is sufficient to trust the analyzer.
#     The earlier 1.5 was over-tuned to clean BT and falsely rejected
#     correct measurements from VIZIO-class soundbars.
MIN_CONFIDENCE_PRIMARY = 3.0
MIN_CONFIDENCE_SECONDARY = 1.2

# Lag-search bounds, anchored on the speaker's CURRENT filter delay.
# We do NOT centre the window on a guessed "expected peak" location:
# the loud chirp + 10 s capture + 4 s post-signal tail give the
# analyzer enough breathing room that it does not need our help
# locating the dominant peak. The window only needs to be wide enough
# to absorb plausible BT inherent variation (100-700 ms across the
# hardware we have seen) and tight enough that distant noise cannot
# masquerade as a real lag.
SEARCH_BELOW_FILTER_MS = 100.0   # search starts this much below current_filter
SEARCH_ABOVE_FILTER_MS = 1500.0  # ...and ends this much above it
PLAUSIBLE_BELOW_FILTER_MS = 30.0  # accepted lag must be at least 30 ms above filter
PLAUSIBLE_ABOVE_FILTER_MS = 1200.0  # ...and at most 1200 ms above (any BT inherent ever)

# Alignment-target safety bounds. We refuse to push more than
# MAX_ADJUSTMENT_MS of single-step adjustment (positive or negative)
# in one calibration cycle; this is a guard against a wildly wrong
# measurement shoving a working speaker far out of sync.
MAX_ADJUSTMENT_MS = 400.0
# Hard clamp on the published user-facing delay value, mirrored from
# ActuationManager.MIN_DELAY_MS / MAX_DELAY_MS so we don't try to
# publish something the actuation chain will silently clamp anyway.
MIN_USER_DELAY_MS = 20.0
MAX_USER_DELAY_MS = 5000.0

EventCallback = Callable[[str, Dict[str, Any]], None]

# BlueZ silently truncates ATT notifications larger than the
# negotiated MTU (~672 -> 669-byte payload). Measured ``applied``
# payload with the full ``actuation`` snapshot is ~700 bytes; the
# truncated form is invalid JSON on the phone and gets dropped. Strip
# the high-volume diagnostic fields for the BLE mirror.
_BLE_DROP_TOP_LEVEL = ("actuation",)
_BLE_MEASUREMENT_KEEP = (
    "lag_ms",
    "confidence_primary",
    "confidence_secondary",
    "sample_rate",
)


def _required_capture_sec(
    current_filter_delay_ms: float,
    calibration_mode: CalibrationMode,
    chirp_duration_sec: Optional[float] = None,
) -> float:
    """Minimum capture window in seconds, given the speaker's current
    filter delay.

    A BT speaker emits the chirp at acoustic time ``filter + inherent``
    after we paplay it into virtual_out. The capture must remain open
    long enough that the WHOLE chirp from the speaker is inside it,
    plus a tail margin for cross-correlation overlap. Without this
    sizing, a speaker that has been pulled up to a Wi-Fi anchor
    (~5 s) would only have a tiny fraction of its chirp captured
    (the original 4.5 s default vs ~4.3 s of pure delay = 0.2 s of
    actual chirp inside the recording), and the analyzer would lock
    onto noise sidelobes with low secondary confidence.

    Music mode is similar but simpler — the reference is continuous,
    so we just need a few seconds of overlap region between
    ``ref[t]`` and ``mic[t + lag]``.

    Worked examples (chirp mode, with the new 4 s tail margin and 10 s floor):
      - filter = 0 ms     → max(10.0, 0 + 0.8 + 2.65 + 0.2 + 4.0) = 10.0 s
      - filter = 3880 ms  → max(10.0, 3.88 + 0.8 + 2.65 + 0.2 + 4.0) = 11.53 s
      - filter = 4960 ms  → max(10.0, 4.96 + 0.8 + 2.65 + 0.2 + 4.0) = 12.61 s

    Worked examples (music mode, with 5 s overlap floor and 10 s capture floor):
      - filter = 0 ms     → max(10.0, 0 + 0.8 + 5.0) = 10.0 s
      - filter = 5000 ms  → max(10.0, 5.0 + 0.8 + 5.0) = 10.8 s
    """
    inherent_sec = BT_INHERENT_MAX_MS / 1000.0
    filter_sec = max(0.0, float(current_filter_delay_ms)) / 1000.0
    if calibration_mode == CALIBRATION_MODE_STARTUP_TUNE:
        chirp = float(chirp_duration_sec) if chirp_duration_sec is not None else 2.65
        required = (
            filter_sec + inherent_sec + chirp
            + PAPLAY_STARTUP_OVERHEAD_SEC + CAPTURE_TAIL_MARGIN_SEC
        )
        return max(DEFAULT_DURATION_STARTUP_SEC, required)
    required = filter_sec + inherent_sec + MUSIC_OVERLAP_MIN_SEC
    return max(DEFAULT_DURATION_SEC, required)


def _lag_search_window(
    current_filter_delay_ms: float,
) -> tuple[float, float, float, float]:
    """Return ``(search_min, search_max, plausible_min, plausible_max)`` in ms.

    The peak the analyzer is looking for is the loud chirp arriving
    at the mic at ``current_filter + inherent_BT_lag``. We do NOT
    pretend to know where exactly that is — instead we hand the
    analyzer a wide window anchored on the speaker's CURRENT filter
    delay, generous enough to absorb any plausible BT inherent lag
    (well under 1200 ms across all hardware we have ever tested) but
    bounded enough that pre-signal noise cannot win.

    The plausibility window is slightly tighter than the search window:
    the analyzer is allowed to *evaluate* a wider region (so it sees
    the noise floor properly) but a winning peak must land in the
    plausibility band to be accepted.

    Worked examples:
      - Fresh BT speaker (filter=0):
          search    = [-50, 1500]
          plausible = [30, 1200]
      - VIZIO post-anchor (filter=4636):
          search    = [4536, 6136]
          plausible = [4666, 5836]

    No "expected peak" centring. The mic + loud chirp + 4 s tail can
    handle the assessment unambiguously inside this window.
    """
    f = float(current_filter_delay_ms)
    search_min = max(-50.0, f - SEARCH_BELOW_FILTER_MS)
    search_max = f + SEARCH_ABOVE_FILTER_MS
    plausible_min = max(PLAUSIBLE_BELOW_FILTER_MS, f + PLAUSIBLE_BELOW_FILTER_MS)
    plausible_max = f + PLAUSIBLE_ABOVE_FILTER_MS
    return search_min, search_max, plausible_min, plausible_max


def _ble_slim_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *payload* small enough to fit a 256–512 byte
    BLE notification regardless of MTU. We round floats to 2 decimals
    and drop the verbose ``actuation`` block (telemetry keeps it)."""
    slim: Dict[str, Any] = {}
    for k, v in payload.items():
        if k in _BLE_DROP_TOP_LEVEL:
            continue
        if k == "measurement" and isinstance(v, dict):
            slim[k] = {
                kk: (round(vv, 2) if isinstance(vv, float) else vv)
                for kk, vv in v.items()
                if kk in _BLE_MEASUREMENT_KEEP
            }
            continue
        if isinstance(v, float):
            slim[k] = round(v, 2)
        else:
            slim[k] = v
    return slim


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


def _paplay_available() -> bool:
    return shutil.which("paplay") is not None


def _mute_sonos_devices(device_ids: List[str], muted: bool) -> List[str]:
    """Best-effort mute / unmute of Sonos devices for calibration isolation.

    Returns the subset that we successfully toggled, so the caller can
    restore exactly those (and not punish users by inadvertently muting a
    Sonos that was already user-muted before the capture started).
    """
    if not device_ids:
        return []
    try:
        from syncsonic_ble.helpers import sonos_controller  # type: ignore
    except ImportError:
        return []
    out: List[str] = []
    for d in device_ids:
        try:
            if sonos_controller.mute(d, muted):
                out.append(d)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Sonos mute] %s -> %s failed: %s", d, muted, exc)
    return out


def _capture_pair(
    duration_sec: float,
    out_dir: Path,
    target_mac: str,
    *,
    play_tune_wav: Optional[Path] = None,
    extra_silence_devices: Optional[List[str]] = None,
) -> Optional[Dict[str, Path]]:
    """Concurrently parecord ``virtual_out.monitor`` and the Jieli mic.

    When ``play_tune_wav`` is set, starts recording first, then plays that
    WAV into ``virtual_out`` via ``paplay`` so the reference tap contains
    the known chirp (pause phone playback for best isolation).

    Otherwise sleeps ``duration_sec`` with whatever audio is already on
    ``virtual_out`` (Slice 4.2 music-driven path).

    ``extra_silence_devices`` is a list of Sonos device IDs to mute over
    SoCo for the duration of the capture. Used by the multi-speaker
    sequence so a Wi-Fi speaker's delayed echo does not contaminate the
    cross-correlation of a BT speaker under test. Muted devices are
    always restored before this function returns.
    """
    mic_source = _resolve_mic_source()
    if not mic_source:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    safe_mac = target_mac.replace(":", "_").lower()
    ref_wav = out_dir / f"ref_{safe_mac}_{ts}.wav"
    mic_wav = out_dir / f"mic_{safe_mac}_{ts}.wav"

    sonos_muted = _mute_sonos_devices(extra_silence_devices or [], True)

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
    t0 = time.monotonic()
    try:
        time.sleep(0.08)
        if play_tune_wav is not None:
            if not _paplay_available():
                return None
            subprocess.run(
                ["paplay", "--device=virtual_out", str(play_tune_wav)],
                capture_output=True,
                timeout=max(120.0, duration_sec + 5.0),
                check=False,
            )
        elapsed = time.monotonic() - t0
        remainder = duration_sec - elapsed
        if remainder > 0:
            time.sleep(remainder)
    finally:
        for p in (ref_proc, mic_proc):
            try:
                p.send_signal(2)  # SIGINT, lets parecord finalise the WAV header
                p.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                p.kill()
            except OSError:
                pass
        # ALWAYS restore Sonos mute state, even on capture failure.
        if sonos_muted:
            _mute_sonos_devices(sonos_muted, False)

    if not ref_wav.exists() or not mic_wav.exists():
        return None
    if ref_wav.stat().st_size < 1024 or mic_wav.stat().st_size < 1024:
        return None
    return {"ref": ref_wav, "mic": mic_wav}


def _calibrate_blocking(
    target_mac: str,
    target_total_ms: float,
    capture_duration_sec: float,
    on_event: EventCallback,
    *,
    calibration_mode: CalibrationMode = CALIBRATION_MODE_MUSIC,
    sequence_index: Optional[int] = None,
    sequence_total: Optional[int] = None,
    extra_silence_devices: Optional[List[str]] = None,
    max_adjustment_ms: Optional[float] = None,
) -> None:
    """The actual calibration steps. Runs on a daemon thread so the
    GLib mainloop is never blocked. ``on_event`` is invoked at each
    phase boundary - the BLE handler wires it to send_notification
    and we additionally mirror to the telemetry events stream."""

    def _emit(phase: str, **fields: Any) -> None:
        payload = {
            "mac": target_mac,
            "phase": phase,
            "calibration_mode": calibration_mode,
            **fields,
        }
        if sequence_index is not None:
            payload["sequence_index"] = sequence_index
        if sequence_total is not None:
            payload["sequence_total"] = sequence_total
        # Telemetry gets the full payload (no size constraint).
        try:
            emit(EventType.CALIBRATION_RESULT, payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("calibrate telemetry emit failed: %s", exc)
        # BLE notify is bounded by the negotiated ATT MTU (typically
        # 672 bytes -> ~669 bytes payload). The unmodified ``applied``
        # event serializes to ~700 bytes and gets silently truncated by
        # BlueZ, producing invalid JSON on the phone. Build a slimmer
        # mirror that drops the verbose ``actuation`` block and rounds
        # measurement floats. The mobile app only needs the outcome.
        ble_payload = _ble_slim_payload(payload)
        try:
            on_event(phase, ble_payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("calibrate on_event callback failed: %s", exc)

    # Resolve the speaker's current filter delay BEFORE sizing the
    # capture window. A speaker already pulled up to a Wi-Fi anchor
    # emits the chirp ~5 s after we paplay it, so the BT-only 4.5 s
    # default would clip the chirp out of the recording entirely. The
    # same value is reused later for the analyzer search window so we
    # don't have to read it twice.
    current_user_delay_ms = _read_published_delay_ms(target_mac)
    if current_user_delay_ms is None:
        current_user_delay_ms = 100.0
    current_filter_delay_ms = max(
        0.0, current_user_delay_ms - ActuationManager.TRANSPORT_BASE_MS,
    )

    tune_path: Optional[Path] = None
    chirp_duration_sec: Optional[float] = None
    if calibration_mode == CALIBRATION_MODE_STARTUP_TUNE:
        try:
            from measurement.startup_tune import ensure_startup_tune_wav, wav_duration_sec
        except ImportError as exc:
            _emit("failed", reason="startup_tune_import_failed", error=str(exc))
            return
        tune_path = ensure_startup_tune_wav()
        chirp_duration_sec = float(wav_duration_sec(tune_path))

    required_sec = _required_capture_sec(
        current_filter_delay_ms, calibration_mode,
        chirp_duration_sec=chirp_duration_sec,
    )
    if capture_duration_sec < required_sec:
        capture_duration_sec = required_sec

    if calibration_mode == CALIBRATION_MODE_STARTUP_TUNE:
        _emit(
            "started",
            target_total_ms=target_total_ms,
            capture_duration_sec=capture_duration_sec,
            startup_tune=str(tune_path),
            pause_phone_audio_hint=True,
            current_filter_delay_ms=round(current_filter_delay_ms, 2),
        )
    else:
        _emit(
            "started",
            target_total_ms=target_total_ms,
            capture_duration_sec=capture_duration_sec,
            current_filter_delay_ms=round(current_filter_delay_ms, 2),
        )

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

    # Wait for the other speakers' A2DP transmit buffers to drain.
    # The C-filter gain hits zero after RAMP_MS (50 ms) but the A2DP
    # stack can hold ~800 ms of already-committed audio that still
    # plays out acoustically. MUTE_SETTLE_SEC (1.0 s) > 50 ms + 800 ms
    # so the room is clean when capture begins.
    time.sleep(MUTE_SETTLE_SEC)

    extras = list(extra_silence_devices or [])
    _emit(
        "capturing",
        duration_sec=capture_duration_sec,
        extra_silence_count=len(extras),
    )
    capture = _capture_pair(
        capture_duration_sec,
        CAPTURE_DIR,
        target_mac,
        play_tune_wav=tune_path,
        extra_silence_devices=extras,
    )

    # Always restore the mutes, even on capture failure - we MUST NOT
    # leave the system silent.
    for sock in others:
        _send_filter_command(sock, f"mute_to 1000 {RAMP_MS}")
    _emit("unmuting_others_done", n_restored=len(others))

    if capture is None:
        reason = "capture_failed"
        if calibration_mode == CALIBRATION_MODE_STARTUP_TUNE and not _paplay_available():
            reason = "paplay_not_available"
        _emit("failed", reason=reason)
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

    # ``current_filter_delay_ms`` was already resolved above so the
    # capture window could be sized correctly. Reuse it now to build
    # the analyzer's search window around the expected peak location:
    # the peak is at ``current_filter_delay + inherent_BT_lag``;
    # without this the analyzer's [-50, 700] default would miss the
    # real peak whenever the speaker has been pulled up to a Wi-Fi
    # anchor.
    search_min, search_max, plausible_min, plausible_max = _lag_search_window(
        current_filter_delay_ms,
    )

    try:
        est = estimate_lag_samples(
            ref_arr, mic_arr, sample_rate=ref_sr,
            min_lag_ms=search_min, max_lag_ms=search_max,
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
        "search_window_ms": [round(search_min, 1), round(search_max, 1)],
        "plausible_window_ms": [round(plausible_min, 1), round(plausible_max, 1)],
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
    elif not (plausible_min <= est.lag_ms <= plausible_max):
        rejection = (
            f"lag_ms {est.lag_ms:.1f} outside plausible window "
            f"[{plausible_min:.1f}, {plausible_max:.1f}]"
        )

    if rejection is not None:
        _emit("failed", reason="rejected_measurement", detail=rejection,
              measurement=measurement)
        return

    # Compute the new user-facing delay. The cross-correlation analyzer
    # returned the TOTAL acoustic lag from virtual_out to the mic, which
    # equals (current filter delay) + (inherent BT chain). To change
    # that total to ``target_total_ms`` we need to shift the FILTER
    # delay by exactly ``(target - measured)`` ms. Filter delay is a
    # pure delay line so the acoustic-lag axis and filter-delay axis
    # are 1:1.
    #
    # IMPORTANT: working in user-delay space (the previous implementation)
    # is only correct when ``user_delay >= TRANSPORT_BASE_MS``. Below that
    # threshold the actuation manager clamps ``filter = max(0, user-base)``
    # to 0, so adjusting user-delay by Δ may produce LESS than Δ of filter
    # change. With a Wi-Fi anchor of ~1700 ms and a starting user-delay
    # at the 20 ms floor, the previous algorithm under-shot the target
    # by exactly ``TRANSPORT_BASE_MS - MIN_USER_DELAY_MS`` (= 100 ms),
    # leaving the BT speaker that much ahead of the Sonos.
    #
    # ``current_user_delay_ms`` and ``current_filter_delay_ms`` were
    # already resolved above so the analyzer search window could be
    # placed around the expected peak.

    adjustment_ms = target_total_ms - est.lag_ms
    effective_max_adjustment = (
        float(max_adjustment_ms)
        if max_adjustment_ms is not None
        else MAX_ADJUSTMENT_MS
    )
    if abs(adjustment_ms) > effective_max_adjustment:
        _emit("failed", reason="adjustment_too_large",
              adjustment_ms=round(adjustment_ms, 2),
              max_adjustment_ms=effective_max_adjustment,
              current_user_delay_ms=round(current_user_delay_ms, 2),
              measurement=measurement)
        return

    new_filter_delay_ms = max(0.0, current_filter_delay_ms + adjustment_ms)
    new_user_delay_ms = new_filter_delay_ms + ActuationManager.TRANSPORT_BASE_MS
    new_user_delay_ms_clamped = max(
        MIN_USER_DELAY_MS, min(MAX_USER_DELAY_MS, new_user_delay_ms),
    )
    clamped = abs(new_user_delay_ms_clamped - new_user_delay_ms) > 1e-3

    _emit("applying",
          current_user_delay_ms=round(current_user_delay_ms, 2),
          current_filter_delay_ms=round(current_filter_delay_ms, 2),
          adjustment_ms=round(adjustment_ms, 2),
          new_filter_delay_ms=round(new_filter_delay_ms, 2),
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
          current_filter_delay_ms=round(current_filter_delay_ms, 2),
          adjustment_ms=round(adjustment_ms, 2),
          new_filter_delay_ms=round(new_filter_delay_ms, 2),
          new_user_delay_ms=round(new_user_delay_ms_clamped, 2),
          target_total_ms=target_total_ms,
          measurement=measurement,
          actuation=snapshot if isinstance(snapshot, dict) else {})


def calibrate_speaker_async(
    target_mac: str,
    on_event: EventCallback,
    target_total_ms: float = DEFAULT_TARGET_TOTAL_MS,
    capture_duration_sec: float = DEFAULT_DURATION_SEC,
    *,
    calibration_mode: CalibrationMode = CALIBRATION_MODE_MUSIC,
    sequence_index: Optional[int] = None,
    sequence_total: Optional[int] = None,
    extra_silence_devices: Optional[List[str]] = None,
    max_adjustment_ms: Optional[float] = None,
) -> threading.Thread:
    """Spawn the calibration on a daemon thread and return immediately.

    ``on_event(phase, payload)`` is invoked from the worker thread at
    each phase boundary. The BLE handler wires this to
    ``Characteristic.send_notification(Msg.CALIBRATION_RESULT, payload)``
    so the mobile app sees live progress + the final result without
    blocking the GLib mainloop on the capture window.

    ``extra_silence_devices`` is a list of Sonos device IDs that should be
    muted for the duration of the capture (and restored afterwards). Used
    by the multi-speaker sequence so Wi-Fi echoes don't pollute the
    cross-correlation of a BT speaker under test.
    """
    t = threading.Thread(
        target=_calibrate_blocking,
        kwargs={
            "target_mac": target_mac,
            "target_total_ms": float(target_total_ms),
            "capture_duration_sec": float(capture_duration_sec),
            "on_event": on_event,
            "calibration_mode": calibration_mode,
            "sequence_index": sequence_index,
            "sequence_total": sequence_total,
            "extra_silence_devices": list(extra_silence_devices) if extra_silence_devices else None,
            "max_adjustment_ms": max_adjustment_ms,
        },
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
    p.add_argument(
        "--mode",
        choices=("music", "startup_tune"),
        default="music",
        help="startup_tune plays the built-in chirp into virtual_out during capture",
    )
    p.add_argument("--duration", type=float, default=None,
                   help="capture window in seconds (default: 6 music / 4.5 startup_tune)")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="wall-clock budget for the whole calibration (default 30s)")
    args = p.parse_args()

    mode: CalibrationMode = "startup_tune" if args.mode == "startup_tune" else "music"
    duration = args.duration
    if duration is None:
        duration = (
            DEFAULT_DURATION_STARTUP_SEC if mode == CALIBRATION_MODE_STARTUP_TUNE else DEFAULT_DURATION_SEC
        )

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
        capture_duration_sec=float(duration),
        calibration_mode=mode,
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
