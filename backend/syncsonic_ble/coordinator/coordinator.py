"""Slice 3 System Coordinator (commit 3.1: observation-only skeleton).

A daemon thread that ticks at TICK_HZ, discovers every live
``pw_delay_filter`` instance via the Unix-socket directory, queries
each one for its current state, and emits one ``coordinator_tick``
event per second to the telemetry stream summarising the system.

This commit deliberately does NOT take any policy action. The
``apply_policy`` method is a no-op stub; subsequent Slice 3 commits
fill it in (3.2 PI rate adjustment, 3.3 system-wide hold, 3.4 soft-
mute on transport failure, 3.5 RSSI-aware preemptive soft-mute).

Why observation-only first
--------------------------
Audio-path correctness is paramount. Adding a policy that drives
``set_rate_ppm`` and ``mute_ramp`` writes back to the filters
introduces nonzero risk of misbehavior on a system the user actively
listens to. Landing the observation skeleton first lets us:

- Verify the tick rate is sustainable on the Pi (CPU cost of per-tick
  socket roundtrips).
- Verify the per-speaker state model populates with sensible numbers
  during real playback.
- Capture a baseline of "natural" queue-depth and frame-rate
  variance, so the Slice 3.2 PI controller's gains and clamp can be
  tuned with data instead of guesswork.

Discovery
---------
We do not depend on the actuation daemon's in-process route table
because that singleton lives in a different process. Instead, we
scan ``/tmp/syncsonic-engine/syncsonic-delay-*.sock`` once per tick.
Sockets are created by the C filter on startup and unlinked on clean
exit; a stale socket whose owning process has died will produce
ECONNREFUSED on connect and be marked as a query failure (state is
retained for 3 ticks then dropped).
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from syncsonic_ble.coordinator.state import (
    HEALTH_HEALTHY,
    HEALTH_MUTED,
    HEALTH_STRESSED,
    SpeakerState,
)
from syncsonic_ble.telemetry import EventType
from syncsonic_ble.telemetry.event_writer import emit
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

SOCKET_DIR = Path("/tmp/syncsonic-engine")
SOCKET_GLOB = "syncsonic-delay-*.sock"
TICK_HZ = 10
TICK_PERIOD_SEC = 1.0 / TICK_HZ
SOCKET_TIMEOUT_SEC = 0.1
EMIT_EVERY_N_TICKS = 10  # 1 event/sec at 10 Hz tick
MAX_QUERY_FAILURES_BEFORE_DROP = 3

# Slice 3.2 soft-mute policy parameters. Tuned conservatively against
# the Slice 3.1 jitter floor of ±10% in the 1s tick window. At 10 Hz
# the natural per-tick frame delta is ~4800 ± ~500 samples; both
# thresholds and the consecutive-tick counts are sized to require a
# clearly-pathological situation, not normal jitter, to trigger.
STRESS_FRAMES_IN_THRESHOLD = 3000   # frames_in/tick still healthy if above this
STRESS_FRAMES_OUT_THRESHOLD = 500   # frames_out/tick is "starved" if below this
TICKS_TO_DECLARE_STRESS = 3          # 300 ms of consecutive stress -> mute
TICKS_TO_DECLARE_RECOVERY = 5        # 500 ms of healthy flow while muted -> unmute
SOFT_MUTE_RAMP_MS = 50               # both fade-out and fade-in
MIN_MUTED_HOLD_MS = 500              # don't unmute earlier than this after muting

_SOCKET_NAME_RE = re.compile(r"^syncsonic-delay-([0-9a-f_]{17})\.sock$")


def _mac_from_socket_filename(filename: str) -> Optional[str]:
    m = _SOCKET_NAME_RE.match(filename)
    if not m:
        return None
    # filename has lower-hex with underscores; convert back to upper-hex with colons
    token = m.group(1).upper().replace("_", ":")
    if len(token) != 17:
        return None
    return token


class Coordinator:
    """Observe-only Coordinator skeleton. Slice 3.2+ adds actions."""

    def __init__(self) -> None:
        self._states: Dict[str, SpeakerState] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._tick_count: int = 0
        self._last_emit_tick: int = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="syncsonic-coordinator",
            daemon=True,
        )
        self._thread.start()
        log.info("Coordinator started (observation-only, %d Hz tick)", TICK_HZ)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._thread = None
        log.info("Coordinator stopped")

    # -- main loop -----------------------------------------------------------

    def _run(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            self._tick()
            self._tick_count += 1
            next_tick += TICK_PERIOD_SEC
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                self._stop.wait(sleep_for)
            else:
                # Tick over-ran its budget; reset deadline.
                next_tick = time.monotonic()

    def _tick(self) -> None:
        macs = self._discover_macs()
        # Drop states for filters that have disappeared and have failed
        # MAX_QUERY_FAILURES_BEFORE_DROP times in a row.
        for mac in list(self._states.keys()):
            if mac not in macs and self._states[mac].n_consecutive_query_failures >= MAX_QUERY_FAILURES_BEFORE_DROP:
                self._states.pop(mac, None)

        for mac in macs:
            sock_path = str(SOCKET_DIR / f"syncsonic-delay-{mac.replace(':', '_').lower()}.sock")
            state = self._states.setdefault(mac, SpeakerState(mac=mac, socket_path=sock_path))
            resp = self._query_filter(sock_path)
            if resp is None:
                state.note_query_failure("connect_or_parse")
                continue
            state.update_from_query(resp)

        # Empty policy slot - subsequent commits fill this.
        self._apply_policy()

        # Emit a compact summary every EMIT_EVERY_N_TICKS.
        if self._tick_count - self._last_emit_tick >= EMIT_EVERY_N_TICKS:
            self._emit_tick_summary()
            self._last_emit_tick = self._tick_count

    def _apply_policy(self) -> None:
        """Slice 3.2: per-speaker soft-mute on transport failure.

        For each speaker we run a 3-state machine:

          HEALTHY  - frames in == frames out, both flowing normally
                     -> on N consecutive stress ticks, mute and go MUTED
          MUTED    - we've sent mute_to 0 50, output is silent
                     -> on M consecutive recovery ticks, unmute and go HEALTHY
                     -> stays muted indefinitely if recovery never comes
          STRESSED - reserved for future use (3.3 RSSI dip / 3.4 system-hold);
                     not entered by this commit's logic

        The detection rule for entering MUTED:
          delta_frames_in  > STRESS_FRAMES_IN_THRESHOLD  (audio still flowing in)
          AND
          delta_frames_out < STRESS_FRAMES_OUT_THRESHOLD (BlueZ has stopped consuming)

        Pause / silence does NOT trigger this because the filter still
        processes zero-valued samples on both sides; both deltas drop
        together and stay in lockstep.
        """
        for state in self._states.values():
            self._tick_speaker_policy(state)

    def _tick_speaker_policy(self, s: SpeakerState) -> None:
        # Skip speakers we have not yet had a successful query for.
        if s.last_query_monotonic_ns == 0:
            return

        in_flowing = s.delta_frames_in > STRESS_FRAMES_IN_THRESHOLD
        out_starved = s.delta_frames_out < STRESS_FRAMES_OUT_THRESHOLD
        in_starved = s.delta_frames_in < STRESS_FRAMES_OUT_THRESHOLD

        if s.health == HEALTH_HEALTHY:
            if in_flowing and out_starved:
                s.consecutive_stress_ticks += 1
                if s.consecutive_stress_ticks >= TICKS_TO_DECLARE_STRESS:
                    self._enter_muted(s, reason="frames_in_flowing_out_starved")
            else:
                s.consecutive_stress_ticks = 0

        elif s.health == HEALTH_MUTED:
            # Recover when frames are clearly flowing through the filter
            # again. We track frames_out (not frames_in) because that's
            # the BlueZ-side health signal; frames_in keeps flowing
            # regardless of the speaker.
            if s.delta_frames_out > STRESS_FRAMES_IN_THRESHOLD and not in_starved:
                s.consecutive_recovery_ticks += 1
                muted_for_ms = (time.monotonic_ns() - s.health_state_entered_monotonic_ns) // 1_000_000
                if (
                    s.consecutive_recovery_ticks >= TICKS_TO_DECLARE_RECOVERY
                    and muted_for_ms >= MIN_MUTED_HOLD_MS
                ):
                    self._exit_muted(s, reason="frames_out_recovered")
            else:
                s.consecutive_recovery_ticks = 0

    def _enter_muted(self, s: SpeakerState, reason: str) -> None:
        ok = self._send_command(s.socket_path, f"mute_to 0 {SOFT_MUTE_RAMP_MS}")
        s.health = HEALTH_MUTED
        s.health_state_entered_monotonic_ns = time.monotonic_ns()
        s.consecutive_stress_ticks = 0
        s.consecutive_recovery_ticks = 0
        log.info(
            "Coordinator: %s -> MUTED (reason=%s, command_ok=%s)",
            s.mac, reason, ok,
        )
        emit(EventType.COORDINATOR_SOFT_MUTE, {
            "mac": s.mac,
            "phase": "mute",
            "reason": reason,
            "ramp_ms": SOFT_MUTE_RAMP_MS,
            "command_ok": ok,
            "delta_frames_in": s.delta_frames_in,
            "delta_frames_out": s.delta_frames_out,
        })

    def _exit_muted(self, s: SpeakerState, reason: str) -> None:
        ok = self._send_command(s.socket_path, f"mute_to 1000 {SOFT_MUTE_RAMP_MS}")
        s.health = HEALTH_HEALTHY
        s.health_state_entered_monotonic_ns = time.monotonic_ns()
        s.consecutive_stress_ticks = 0
        s.consecutive_recovery_ticks = 0
        log.info(
            "Coordinator: %s -> HEALTHY (reason=%s, command_ok=%s)",
            s.mac, reason, ok,
        )
        emit(EventType.COORDINATOR_SOFT_MUTE, {
            "mac": s.mac,
            "phase": "unmute",
            "reason": reason,
            "ramp_ms": SOFT_MUTE_RAMP_MS,
            "command_ok": ok,
            "delta_frames_in": s.delta_frames_in,
            "delta_frames_out": s.delta_frames_out,
        })

    # -- helpers -------------------------------------------------------------

    def _discover_macs(self) -> List[str]:
        if not SOCKET_DIR.exists():
            return []
        out: List[str] = []
        try:
            for p in SOCKET_DIR.glob(SOCKET_GLOB):
                mac = _mac_from_socket_filename(p.name)
                if mac:
                    out.append(mac)
        except OSError as exc:
            log.debug("Coordinator socket dir scan failed: %s", exc)
        return out

    def _query_filter(self, sock_path: str) -> Optional[Dict[str, object]]:
        return self._send_socket_command(sock_path, "query")

    def _send_command(self, sock_path: str, line: str) -> bool:
        """Send one command to a filter socket; return True if the response
        parsed as ``{"ok":true,...}``. Used by the soft-mute policy."""
        resp = self._send_socket_command(sock_path, line)
        return bool(resp and resp.get("ok"))

    def _send_socket_command(self, sock_path: str, line: str) -> Optional[Dict[str, object]]:
        if not os.path.exists(sock_path):
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(SOCKET_TIMEOUT_SEC)
                s.connect(sock_path)
                s.sendall((line + "\n").encode("ascii"))
                buf = b""
                while b"\n" not in buf and len(buf) < 4096:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
        except (OSError, socket.timeout):
            return None
        try:
            return json.loads(buf.decode("ascii", errors="replace").strip().split("\n")[0])
        except (ValueError, json.JSONDecodeError):
            return None

    def _emit_tick_summary(self) -> None:
        # Compact per-speaker payload + global counters. The events
        # stream is the analyzer's window into Coordinator behavior;
        # keep per-tick volume small.
        speakers = [s.to_event_payload() for s in self._states.values()]
        emit(EventType.COORDINATOR_TICK, {
            "tick_count": self._tick_count,
            "tick_hz": TICK_HZ,
            "n_speakers": len(self._states),
            "policy_actions_enabled": True,  # Slice 3.2: soft-mute live
            "speakers": speakers,
        })


# Process-wide singleton
_COORDINATOR: Optional[Coordinator] = None


def get_coordinator() -> Optional[Coordinator]:
    return _COORDINATOR


def build_and_start_coordinator() -> Coordinator:
    global _COORDINATOR
    if _COORDINATOR is None:
        _COORDINATOR = Coordinator()
    _COORDINATOR.start()
    return _COORDINATOR
