# Epic 05: Coordinated Engine

_This epic is the active workstream of the Now horizon. The strategic
context (why this epic exists, how it relates to the Mid and Long
horizons, and the design principles every slice is held to) lives in
[`../ROADMAP.md`](../ROADMAP.md). Read that first if you are starting
or merging anything substantial here._

## Goal

Make multi-Bluetooth (and later Wi-Fi) speaker playback feel seamless and
reliable on the existing Raspberry Pi hardware by replacing the current
per-speaker, fixed-delay, teardown-on-stress pipeline with a coordinated
elastic-buffer engine driven by a single system-wide policy.

The bar for "done" is that brief Bluetooth transport stress on any one
speaker is hidden from the listener — no audible click on the affected
speaker, no perceptible drift between speakers, no hard disconnect.

## Why This Lane Exists

The four prior epics (01–04) each assumed the existing PipeWire delay
transport was a stable substrate to build on top of. Live evidence on the
Pi shows it is not:

- per-speaker delay changes cause graph xruns every time
- a single speaker A2DP transport failure tears the route down with no
  retry and propagates a visible underrun to the other speakers
- the JSON-on-disk control plane silently diverges from PipeWire reality
  and spins warning loops when state goes stale
- there is no closed-loop convergence between speakers; drift accumulates
  until the user manually nudges the slider, which itself causes an xrun

The mic auto-alignment epics (02 and 03) cannot deliver value until the
underlying transport stops introducing the artifacts they would correct.

## In Scope

- a coordinator process that replaces `pipewire_actuation_daemon.py` and
  owns per-speaker state in-process (no JSON polling)
- a per-speaker engine that replaces `pw_delay_filter.c` with one stereo
  process per speaker, a deeper elastic ring (~400 ms), and a Unix-socket
  control surface accepting smooth `set_delay`, `set_rate_ppm`, `pad`,
  and `mute_ramp` commands without restart
- system-wide synchronous hold and bounded per-speaker rate adjustments
  as the primary jitter-handling mechanism
- soft-mute + phase-aligned re-entry on transport failure instead of
  speaker disconnect
- an always-on telemetry stream (jsonl) that joins audio capture, BlueZ
  events, and per-speaker queue depths against `CLOCK_MONOTONIC` so every
  later change can be measured
- waking the already-installed USB measurement microphone as the source of
  truth for both runtime correction and validation
- the small bug-fix triage that today makes the system lie to itself
  (phone MAC leaking into the speaker control plane, daemon spinning a
  warning loop on offline speakers, priority.driver set in three places)

## Out of Scope

- new Bluetooth controllers or any custom hardware
- replacement of BlueZ A2DP, kernel, or the Pi-class SoC
- ultrasonic alignment as a standalone feature (it becomes a downstream
  consumer of the coordinator once Slice 4 lands)
- Wi-Fi speaker support (it becomes a downstream output driver once the
  engine and coordinator are stable)

## Starting Point

Begin from `foundation/neutral-minimal`. Reuse the BLE GATT control plane,
the BlueZ FSM in `connection_manager.py`, the per-MAC adapter scheduler in
`action_planning.py`, and the existing `pw_delay_filter.c` ring-buffer
shape as the seed for the new engine. The deeper hybrid loopback
experiment that lives on `wip/01-pipewire-transport-phone-ingress` and on
several historical branches is intentionally not carried forward — the
live evidence on the Pi shows it never deployed and the simpler direct
chain is a better starting point.

The full architectural rationale, evidence from the Pi, and the
slice-by-slice implementation plan live in
[`docs/maverick/proposals/05-coordinated-engine-architecture.md`](../proposals/05-coordinated-engine-architecture.md).

## Slice Plan

| Slice | Outcome | Approximate effort |
|---|---|---|
| 0 | Bug-fix triage (phone-MAC guard, no-spin on offline speakers, single-source priority.driver, one-shot auto-reconnect) and ship the WirePlumber rule to foundation | 1 day |
| 1 | Telemetry layer + always-on mic capture + reproducible session report | 1 week |
| 2 | Stereo elastic delay engine with Unix-socket IPC; smooth in-place delay/rate changes with no xrun | 2 weeks |
| 3 | System Coordinator with bounded rate adjustment, system-wide hold, soft-mute + phase-aligned re-entry | 2 weeks |
| 4 | Mic-driven runtime alignment as a coordinator client | 2 weeks |

## Validation Expectations

- local backend syntax/test checks (`python -m compileall syncsonic_ble`)
- Raspberry Pi validation is mandatory for every slice
- per the validation skill in `.agents/skills/pi-hardware-verify/SKILL.md`,
  every slice deploys to the Pi and produces a measurable, reproducible
  before/after on the Slice 1 telemetry stream
- a slice is not "done" until the same fixed 30-second music sample,
  played through the system, produces a session report whose dropout and
  inter-speaker drift numbers meet the slice's stated success criterion
