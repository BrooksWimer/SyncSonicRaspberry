# SyncSonic Maverick Workstream Model

This document defines how Maverick should structure SyncSonic workstreams.
For the long-term strategic roadmap (North Star, time horizons, design
principles, open questions carried forward) see
[`ROADMAP.md`](ROADMAP.md).

## Production Base

`main` (created 2026-05-05 from `epic/05-coordinated-engine`, Pi-validated identical to deployed reality) is the production branch. Every new Maverick workstream for SyncSonic branches from `main`, attaches to one of the ten durable lanes, and finishes back into its lane branch. Only the lane itself gets explicitly promoted to `main`.

The historical `foundation/neutral-minimal` branch is no longer the base. It's preserved in git as a reference for the pre-coordinated-engine Bluetooth-only stack, but new work doesn't branch from it.

## Lane Merge Targets

Ten first-class lanes, all branching from `main`. Six are post-North-Star (defined 2026-05-05 in the branch-model rewrite); four are historical epic lanes elevated to first-class status on 2026-05-09 because their v1 ships in `main` via the coordinated engine but each one still owns ongoing forward work:

| Lane | Branch | Source | Purpose |
|---|---|---|---|
| `feature-hardening` | `feature-hardening` | post-rewrite | Stress-test the coordinated engine, scenario coverage, recovery paths |
| `ui-polish` | `ui-polish` | post-rewrite | Mobile/Expo polish, fit-and-finish |
| `custom-hardware-design` | `custom-hardware-design` | post-rewrite | Custom enclosure, board, BOM (mostly design, not Pi-deploying) |
| `patent-application` | `patent-application` | post-rewrite | Patent drafting + prior-art research |
| `ultrasonic-runtime-sync` | `ultrasonic-runtime-sync` | post-rewrite | Port ultrasonic playback-time correction proofs into coordinated engine |
| `spatial-audio-awareness` | `spatial-audio-awareness` | post-rewrite | Exploratory: room mapping, mic-driven shape adjustment |
| `pipewire-stability` | `pipewire-stability` | elevated 2026-05-09 | Ongoing PipeWire deterministic transport under codec swaps + drift |
| `startup-mic` | `startup-mic` | elevated 2026-05-09 | Chirp + music anchor calibration improvements |
| `runtime-ultrasonic` | `runtime-ultrasonic` | elevated 2026-05-09 | Historical experimentation lane; preserved alongside ultrasonic-runtime-sync |
| `wifi-manual` | `wifi-manual` | elevated 2026-05-09 | Wi-Fi speakers beyond Sonos (TV, other targets) |

The old `epic/01..05` branches stay in git as historical references. Do **not** branch new work from them.

If a Maverick workstream spans multiple lanes, split it into separate workstreams or sequence them explicitly.

## Historical Branches

These branches are research sources and code mines, not merge targets:

- `epic/01-pipewire-transport-stability`, `epic/02-startup-mic-auto-alignment`, `epic/03-runtime-ultrasonic-auto-alignment`, `epic/04-wifi-speakers-manual-alignment`, `epic/05-coordinated-engine` — the five historical epic branches; their content is in `main` via the coordinated engine merge on 2026-05-05.
- `foundation/neutral-minimal` — pre-coordinated-engine Bluetooth-only base.
- `pi-stable-baseline-2026-04-05` — pre-North-Star deployable snapshot.
- `pipewire-delay-transport`, `pipewire-calibration-profile`, `pipewire-redesign` — research lanes.
- `wip/full-diff-snapshot-2026-03-11` — debugging snapshot.

Do not merge them wholesale into an active lane. Manually transplant only the code that belongs.

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
