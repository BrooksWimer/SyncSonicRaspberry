"""Per-speaker state model for the Slice 3 Coordinator.

A SpeakerState is a small mutable dataclass the Coordinator updates on
every tick. Subsequent commits in Slice 3 add fields here as the
policies that need them are added (rate_ppm history, RSSI window,
consecutive_stress_ms, last_xrun_ts, etc.).

This commit (3.1) is observation-only: only the fields populated from
the filter's `query` socket response are tracked.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


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
    # Used by Slice 3.2's PI controller to detect input/output rate
    # imbalance independently of the filter's self-reported queue depth.
    last_frames_in_total: int = 0
    last_frames_out_total: int = 0
    delta_frames_in: int = 0
    delta_frames_out: int = 0

    # Health bookkeeping populated by Slice 3.3+. Default zero.
    consecutive_stress_ms: int = 0
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
        self.mute_ramp_remaining = int(resp.get("mute_ramp_remaining", 0))
        self.ring_capacity = int(resp.get("ring_capacity", 0))

        self.delta_frames_in = max(0, self.frames_in_total - self.last_frames_in_total)
        self.delta_frames_out = max(0, self.frames_out_total - self.last_frames_out_total)
        self.n_consecutive_query_failures = 0
        self.last_failure_reason = ""

    def note_query_failure(self, reason: str) -> None:
        self.n_consecutive_query_failures += 1
        self.last_failure_reason = reason

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
            "mute_ramp_remaining": self.mute_ramp_remaining,
            "consecutive_stress_ms": self.consecutive_stress_ms,
            "n_consecutive_query_failures": self.n_consecutive_query_failures,
        }
