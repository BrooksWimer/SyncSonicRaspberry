"""Per-speaker state model for the Slice 3 Coordinator.

A SpeakerState is a small mutable dataclass the Coordinator updates on
every tick. Slice 3.1 added the observation fields populated from the
filter's ``query`` socket response. Slice 3.2 added the soft-mute
state machine (HEALTHY <-> STRESSED <-> MUTED). Slice 3.3 adds RSSI
fields refreshed from the RssiSampler's shared snapshot, plus a
second stress counter for the RSSI-dip preemptive trigger.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


# Health state machine (Slice 3.2).
HEALTH_HEALTHY = "healthy"          # frames flowing in == out, no recent stress
HEALTH_STRESSED = "stressed"        # frames_in flowing but frames_out lagging; about to soft-mute
HEALTH_MUTED = "muted"              # we soft-muted the speaker; waiting for transport to recover


@dataclass
class SpeakerState:
    mac: str
    socket_path: str

    # Last successful query response timestamps + counters.
    last_query_monotonic_ns: int = 0
    last_query_wall_unix: float = 0.0

    # Filter-reported telemetry, atomic snapshot per query.
    target_delay_samples: int = 0
    current_delay_samples_x100: int = 0     # /100 to get fractional samples
    rate_ppm: int = 0
    queue_depth_samples: int = 0
    frames_in_total: int = 0
    frames_out_total: int = 0
    mute_ramp_remaining: int = 0
    ring_capacity: int = 0

    # Derived per-tick: difference of frames_in_total since last tick.
    # The Slice 3.2 stress detector uses these directly: a stressed
    # speaker has delta_frames_in > IN_THRESHOLD (audio still flowing
    # in from virtual_out) AND delta_frames_out < OUT_THRESHOLD
    # (BlueZ has stopped consuming).
    last_frames_in_total: int = 0
    last_frames_out_total: int = 0
    delta_frames_in: int = 0
    delta_frames_out: int = 0

    # Slice 3.2 gain state from query response.
    target_gain_x1000: int = 1000
    current_gain_x1000: int = 1000
    gain_ramp_samples: int = 0

    # Slice 3.2 health state machine.
    health: str = HEALTH_HEALTHY
    consecutive_stress_ticks: int = 0
    consecutive_recovery_ticks: int = 0
    health_state_entered_monotonic_ns: int = 0

    # Slice 3.3 RSSI tracking (refreshed each Coordinator tick from the
    # RssiSampler's shared snapshot, NOT from the filter query). The
    # rssi_dip_db delta is the primary stress signal: median_60s acts
    # as the long-term baseline, median_10s as the recent state, and a
    # positive delta means the link has degraded.
    latest_rssi_dbm: int = 0
    rssi_median_10s: float = 0.0
    rssi_median_60s: float = 0.0
    rssi_n_samples_10s: int = 0
    rssi_n_samples_60s: int = 0
    last_rssi_monotonic_ns: int = 0
    consecutive_rssi_stress_ticks: int = 0

    # General bookkeeping
    n_consecutive_query_failures: int = 0

    # Last query response that failed to parse / connect; useful for
    # debugging without flooding the journal.
    last_failure_reason: str = ""

    def update_from_query(self, resp: dict) -> None:
        """Apply one ``query`` socket response to this state."""
        self.last_query_monotonic_ns = time.monotonic_ns()
        self.last_query_wall_unix = time.time()

        # Stash previous totals so the Coordinator can compute deltas.
        self.last_frames_in_total = self.frames_in_total
        self.last_frames_out_total = self.frames_out_total

        self.target_delay_samples = int(resp.get("target_delay_samples", 0))
        self.current_delay_samples_x100 = int(resp.get("current_delay_samples_x100", 0))
        self.rate_ppm = int(resp.get("rate_ppm", 0))
        self.queue_depth_samples = int(resp.get("queue_depth_samples", 0))
        self.frames_in_total = int(resp.get("frames_in_total", 0))
        self.frames_out_total = int(resp.get("frames_out_total", 0))
        self.target_gain_x1000 = int(resp.get("target_gain_x1000", 1000))
        self.current_gain_x1000 = int(resp.get("current_gain_x1000", 1000))
        self.gain_ramp_samples = int(resp.get("gain_ramp_samples", 0))
        self.ring_capacity = int(resp.get("ring_capacity", 0))

        self.delta_frames_in = max(0, self.frames_in_total - self.last_frames_in_total)
        self.delta_frames_out = max(0, self.frames_out_total - self.last_frames_out_total)
        self.n_consecutive_query_failures = 0
        self.last_failure_reason = ""

    def note_query_failure(self, reason: str) -> None:
        self.n_consecutive_query_failures += 1
        self.last_failure_reason = reason

    @property
    def rssi_dip_db(self) -> float:
        """Positive value => recent (10s) RSSI is worse than baseline (60s).

        Returns 0.0 if we don't have a meaningful baseline yet (fewer
        than ~10 samples in the 60s deque); the Coordinator's
        thresholding uses the sample-count check before this anyway,
        but returning 0.0 keeps event payloads honest.
        """
        if self.rssi_n_samples_60s < 10:
            return 0.0
        return self.rssi_median_60s - self.rssi_median_10s

    def to_event_payload(self) -> dict:
        """Compact dict for emission as a coordinator_tick event.

        Keep this small - the Coordinator emits one event per tick so
        the volume matters; expand only when a Slice 3.x commit
        actually needs more fields in the report.
        """
        return {
            "mac": self.mac,
            "target_delay_samples": self.target_delay_samples,
            "current_delay_samples_x100": self.current_delay_samples_x100,
            "rate_ppm": self.rate_ppm,
            "queue_depth_samples": self.queue_depth_samples,
            "frames_in_total": self.frames_in_total,
            "frames_out_total": self.frames_out_total,
            "delta_frames_in": self.delta_frames_in,
            "delta_frames_out": self.delta_frames_out,
            "target_gain_x1000": self.target_gain_x1000,
            "current_gain_x1000": self.current_gain_x1000,
            "health": self.health,
            "consecutive_stress_ticks": self.consecutive_stress_ticks,
            "consecutive_recovery_ticks": self.consecutive_recovery_ticks,
            "consecutive_rssi_stress_ticks": self.consecutive_rssi_stress_ticks,
            "latest_rssi_dbm": self.latest_rssi_dbm,
            "rssi_median_10s": round(self.rssi_median_10s, 1),
            "rssi_median_60s": round(self.rssi_median_60s, 1),
            "rssi_dip_db": round(self.rssi_dip_db, 1),
            "rssi_n_samples_60s": self.rssi_n_samples_60s,
            "n_consecutive_query_failures": self.n_consecutive_query_failures,
        }
