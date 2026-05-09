# Epic: pipewire-stability

A first-class lane for ongoing PipeWire / WirePlumber transport stability work.

## Status

v1 of PipeWire transport stability was implemented and folded into the coordinated engine (the multi-speaker delay filter, route management, and the elastic-buffer scheduling). That is **the floor**, not the ceiling — this lane owns ongoing work to keep the audio pipeline deterministic as conditions change.

The original epic/01-pipewire-transport-stability branch (now historical) preserves the v1 development trail.

## Goal

Keep the PipeWire-side audio path deterministic and predictable across:

- WirePlumber config drift between distro versions
- Codec swaps (SBC ↔ aptX ↔ LDAC) on the same speaker
- Module/profile churn during BlueZ reconnects
- USB audio sink behavior when the host wakes from sleep
- New PipeWire releases (currently pinned to 1.2.7 — this lane drives upgrades)

## In scope

- PipeWire/WirePlumber upgrade work and the test plan that decides whether an upgrade is safe
- New transport-mode experiments (e.g., quieter pre-roll, alternative buffering strategies)
- Diagnostic tooling that surfaces transport-state drift faster than the current journal-grep workflow
- Edge-case fixes for `pw_delay_filter` under sustained backpressure
- Coordination with `feature-hardening` when a stability bug surfaces from soak testing

## Out of scope

- Audio routing inside the coordinated engine itself (lives in `feature-hardening` if it's a robustness concern, or in the engine's existing structure if it's a feature)
- BLE / control-plane stability (those are `feature-hardening` and the BLE-layer code respectively)
- UI-side telemetry visualization (`ui-polish`)

## Discord

Lane Discord thread: see `discord_thread_bindings` for the current routing.

## Notes

- The historical `epic/01-pipewire-transport-stability` branch is preserved as a reference.
- New work on this lane branches from `main` (current production with all the v1 work merged in), not from the historical branch.
