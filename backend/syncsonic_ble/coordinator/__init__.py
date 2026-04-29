"""SyncSonic Slice 3 System Coordinator package.

This package owns the system-level audio-coordination policy. The
Coordinator is a small daemon thread inside ``syncsonic_ble.main`` that
ticks at ~10 Hz, queries every live ``pw_delay_filter`` instance via
its Unix-socket control surface, builds an in-process model of each
speaker's queue health, and applies bounded corrections back to the
filters.

The architecture proposal Section 4.3 defines three policy primitives;
the Coordinator implements them in successive commits inside Slice 3:

1. **Bounded rate adjustment (the "fast lane")** - per-speaker PI
   controller targeting 50% queue depth, output clamped to ±50 ppm.
   Inaudible per the Section 9 implications.
2. **System-wide synchronous hold (the "panic lane")** - when any one
   speaker's queue starts draining faster than its rate adjustment can
   compensate, slow EVERY speaker by -20 ppm for ~200 ms. All speakers
   move together; no inter-speaker drift is perceptible because every
   listener gets the same global stretch.
3. **Soft-mute + phase-aligned re-entry (the "graceful failure")** -
   on real transport failure, ramp the failing speaker to silence over
   30-50 ms (perceived as fade-out, not click), attempt A2DP
   re-establish without RemoveDevice, ramp back in. Other speakers
   carry the experience.

Process model
-------------
The Coordinator runs as a daemon thread inside ``syncsonic_ble.main``,
NOT inside the existing ``pipewire_actuation_daemon`` process. Two
reasons:

- The actuation daemon's job is "translate JSON control state into
  ensure_route calls." That's a transport responsibility, not a
  policy responsibility. The Coordinator is the policy layer that
  rides ON TOP of the transport, observing and correcting.
- Coordinator failure must never break audio. Living inside the BLE
  service means it shares fate with everything else; the actuation
  daemon stays a separate-process safety boundary that the Coordinator
  cannot accidentally crash.

The Coordinator reaches into the running pw_delay_filter processes via
their Unix sockets (``/tmp/syncsonic-engine/<node_name>.sock``)
directly. It does NOT depend on ``transport_manager._active_routes``
because that singleton lives in the actuation_daemon's process, not
ours. Speaker discovery is by globbing the socket directory.
"""

from syncsonic_ble.coordinator.state import SpeakerState

__all__ = ["SpeakerState"]
