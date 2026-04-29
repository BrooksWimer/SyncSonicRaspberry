# SyncSonic Maverick Workstream Model

This document defines how Maverick should structure SyncSonic workstreams.
For the long-term strategic roadmap (North Star, time horizons, design
principles, open questions carried forward) see
[`ROADMAP.md`](ROADMAP.md).

## Neutral Foundation

- `foundation/neutral-minimal` is the stable base for all new feature work.
- It is intentionally Bluetooth-only with manual delay control and a minimal
  PipeWire delay-node core.
- Do not treat the foundation branch as the place to build new feature work
  unless the task is explicitly about the shared base, repo hygiene, or the
  baseline runtime itself.

## Epic Merge Targets

- `epic/05-coordinated-engine` (active lane; the foundation that the
  other four epics now build on top of — see
  [`ROADMAP.md`](ROADMAP.md))
- `epic/01-pipewire-transport-stability` (downstream of epic/05;
  retrofitted with an Experiment Ledger once the Slice 1 telemetry
  harness exists)
- `epic/02-startup-mic-auto-alignment` (downstream of epic/05; the
  Slice 1 always-on mic capture is the ground-truth oracle this epic
  was missing)
- `epic/03-runtime-ultrasonic-auto-alignment` (downstream of epic/05;
  becomes a coordinator client once Slice 4 exists)
- `epic/04-wifi-speakers-manual-alignment` (downstream of epic/05;
  GCC-PHAT in the analyzer is what was missing here)

Every Maverick workstream for SyncSonic should attach to one of these epic
branches. If a request spans multiple lanes, split it into multiple
workstreams or sequence the work explicitly.

## Historical Branches

These branches are research sources and code mines, not merge targets:

- `pi-stable-baseline-2026-04-05`
- `pipewire-delay-transport`
- `pipewire-calibration-profile`
- `pipewire-redesign`
- `wip/full-diff-snapshot-2026-03-11`

Do not merge them wholesale into an epic branch. Manually transplant only the
code that belongs in the active lane.

## Discord Routing Model

- Each epic should have its own Discord channel route pinned to its `epicId`.
- Start new workstreams inside the routed epic channel when possible.
- If a workstream is started elsewhere, pass the explicit `epic` option.
- Avoid a shared catch-all feature-development channel for SyncSonic work.

## Cross-Epic Rules

- Keep Wi-Fi speaker work separate from microphone automation until manual
  alignment behavior is understood and validated.
- Keep startup microphone calibration separate from runtime ultrasonic
  correction.
- Keep microphone epics publishing targets through the shared actuation and
  control-plane boundary instead of bypassing the transport layer directly.
- Use Raspberry Pi validation for any claim about BLE behavior, audio routing,
  latency control, startup/runtime audio services, or end-to-end alignment.
