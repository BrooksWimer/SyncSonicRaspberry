"""Slice 3 System Coordinator.

A daemon thread that ticks at TICK_HZ, discovers every live
``pw_delay_filter`` instance via the Unix-socket directory, queries
each one for its current state, refreshes per-speaker RSSI from the
RssiSampler's shared snapshot, and emits one ``coordinator_tick``
event per second to the telemetry stream summarising the system.

Active policy as of Slice 3.3:
- HEALTHY -> MUTED on N consecutive ticks of "frames_in flowing AND
  frames_out starved" (Slice 3.2: transport-failure detector).
- HEALTHY -> MUTED on N consecutive ticks of "10s rolling-median
  RSSI is RSSI_DIP_THRESHOLD_DB or more below the 60s baseline"
  (Slice 3.3: preemptive RSSI dip detector).
- MUTED -> HEALTHY on M consecutive ticks of "frames_out clearly
  flowing again" + 500 ms minimum hold to debounce.

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
from syncsonic_ble.telemetry.samplers.rssi_sampler import (
    RssiSnapshot,
    get_latest_rssi,
)
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

# Slice 3.3 RSSI-aware preemptive policy parameters. RSSI is the
# leading indicator: by the time delta_frames_out collapses (Slice
# 3.2's signal), the audible dropout is already in flight. RSSI
# sampling runs at 1 Hz so the 10s rolling median is a fairly stable
# 10-sample summary. We require RSSI_DIP_THRESHOLD_DB of degradation
# vs the 60s baseline to fire, plus enough samples in BOTH windows
# so the comparison is meaningful (no triggering on a half-empty
# 60s deque immediately after a fresh connection).
RSSI_DIP_THRESHOLD_DB = 5.0          # 10s median this much below 60s baseline => stress
TICKS_TO_DECLARE_RSSI_STRESS = 2     # 200 ms of consecutive RSSI stress -> mute
RSSI_MIN_SAMPLES_10S = 5             # 10s deque must have at least this many samples
RSSI_MIN_SAMPLES_60S = 30            # 60s deque must have at least this many samples
# Note: with a 1 Hz sampler, a fresh connection takes >=30 s before
# the RSSI-dip detector is allowed to fire. That's deliberate; we
# would rather miss the first 30 s of RSSI-stress on a brand-new
# connection than false-mute on a deque that hasn't filled yet.

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
        log.info(
            "Coordinator started (policy=soft-mute[transport+rssi], %d Hz tick)",
            TICK_HZ,
        )

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
                # Even when the filter query fails we still refresh
                # RSSI; otherwise the dip detector goes blind exactly
                # when we most need it.
                self._refresh_rssi(state)
                continue
            state.update_from_query(resp)
            self._refresh_rssi(state)

        self._apply_policy()

        # Emit a compact summary every EMIT_EVERY_N_TICKS.
        if self._tick_count - self._last_emit_tick >= EMIT_EVERY_N_TICKS:
            self._emit_tick_summary()
            self._last_emit_tick = self._tick_count

    def _apply_policy(self) -> None:
        """Per-speaker soft-mute policy.

        For each speaker we run a 3-state machine:

          HEALTHY  - frames in == frames out, both flowing normally
                     -> N consecutive frame-stuck ticks  -> MUTED  (Slice 3.2)
                     -> N consecutive RSSI-dip ticks      -> MUTED  (Slice 3.3)
          MUTED    - we've sent mute_to 0 50, output is silent
                     -> M consecutive recovery ticks    -> HEALTHY
                     -> stays muted indefinitely if recovery never comes
          STRESSED - reserved for future system-hold use (Slice 3.4);
                     not entered by current logic

        Detection rule 1 (Slice 3.2 - reactive, transport-failure):
          delta_frames_in  > STRESS_FRAMES_IN_THRESHOLD  (audio still flowing in)
          AND
          delta_frames_out < STRESS_FRAMES_OUT_THRESHOLD (BlueZ has stopped consuming)

        Detection rule 2 (Slice 3.3 - preemptive, RSSI-dip):
          rssi_median_60s - rssi_median_10s >= RSSI_DIP_THRESHOLD_DB
          AND enough samples in both deques to make the comparison meaningful.

        Either rule firing transitions HEALTHY -> MUTED. Recovery is
        unified: only frame-flow recovery exits MUTED, since RSSI may
        recover before BlueZ has actually drained its retransmit queue.

        Pause / silence does NOT trigger rule 1 because the filter
        still processes zero-valued samples on both sides; both
        deltas drop together. Rule 2 is unaffected by playback state.
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

        # Slice 3.3 RSSI-dip detector. Only meaningful once both deques
        # have filled enough; until then we just don't increment the
        # counter (so a fresh connection can't false-mute itself in
        # its first 30 seconds).
        rssi_dip = (
            s.rssi_n_samples_10s >= RSSI_MIN_SAMPLES_10S
            and s.rssi_n_samples_60s >= RSSI_MIN_SAMPLES_60S
            and s.rssi_dip_db >= RSSI_DIP_THRESHOLD_DB
        )

        if s.health == HEALTH_HEALTHY:
            # Frame-stuck detector (Slice 3.2)
            if in_flowing and out_starved:
                s.consecutive_stress_ticks += 1
            else:
                s.consecutive_stress_ticks = 0

            # RSSI-dip detector (Slice 3.3)
            if rssi_dip:
                s.consecutive_rssi_stress_ticks += 1
            else:
                s.consecutive_rssi_stress_ticks = 0

            # Whichever fires first wins; tie goes to frame-stuck since
            # it's the more deterministic "audio is actively dying"
            # signal.
            if s.consecutive_stress_ticks >= TICKS_TO_DECLARE_STRESS:
                self._enter_muted(s, reason="frames_in_flowing_out_starved")
            elif s.consecutive_rssi_stress_ticks >= TICKS_TO_DECLARE_RSSI_STRESS:
                self._enter_muted(s, reason="rssi_dip")

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
        s.consecutive_rssi_stress_ticks = 0
        s.consecutive_recovery_ticks = 0
        log.info(
            "Coordinator: %s -> MUTED (reason=%s, rssi_dip_db=%.1f, command_ok=%s)",
            s.mac, reason, s.rssi_dip_db, ok,
        )
        emit(EventType.COORDINATOR_SOFT_MUTE, {
            "mac": s.mac,
            "phase": "mute",
            "reason": reason,
            "ramp_ms": SOFT_MUTE_RAMP_MS,
            "command_ok": ok,
            "delta_frames_in": s.delta_frames_in,
            "delta_frames_out": s.delta_frames_out,
            "latest_rssi_dbm": s.latest_rssi_dbm,
            "rssi_median_10s": round(s.rssi_median_10s, 1),
            "rssi_median_60s": round(s.rssi_median_60s, 1),
            "rssi_dip_db": round(s.rssi_dip_db, 1),
        })

    def _exit_muted(self, s: SpeakerState, reason: str) -> None:
        ok = self._send_command(s.socket_path, f"mute_to 1000 {SOFT_MUTE_RAMP_MS}")
        s.health = HEALTH_HEALTHY
        s.health_state_entered_monotonic_ns = time.monotonic_ns()
        s.consecutive_stress_ticks = 0
        s.consecutive_rssi_stress_ticks = 0
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
            "latest_rssi_dbm": s.latest_rssi_dbm,
            "rssi_dip_db": round(s.rssi_dip_db, 1),
        })

    # -- helpers -------------------------------------------------------------

    def _refresh_rssi(self, s: SpeakerState) -> None:
        """Pull the freshest RssiSampler snapshot for this speaker into
        the SpeakerState. Returning silently when no sample is available
        keeps the policy detector blind (rssi_n_samples_10s stays 0,
        which fails the threshold check), exactly the safe behaviour we
        want before the sampler has caught up to a fresh connection.
        """
        snap: Optional[RssiSnapshot] = get_latest_rssi(s.mac)
        if snap is None:
            return
        s.latest_rssi_dbm = snap.latest_dbm
        s.rssi_median_10s = snap.median_10s
        s.rssi_median_60s = snap.median_60s
        s.rssi_n_samples_10s = snap.n_samples_10s
        s.rssi_n_samples_60s = snap.n_samples_60s
        s.last_rssi_monotonic_ns = snap.last_sample_monotonic_ns

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
