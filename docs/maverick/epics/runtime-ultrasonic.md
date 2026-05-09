# Epic: runtime-ultrasonic

A first-class lane for runtime ultrasonic auto-alignment work.

## Status

v1 of runtime ultrasonic experimentation lives in the historical epic/03 branch — proofs that mid-playback ultrasonic measurement is viable for drift correction. That work is preserved as a reference, not a deployable artifact.

This lane is **distinct from but adjacent to** [`ultrasonic-runtime-sync`](ultrasonic-runtime-sync.md):

- `runtime-ultrasonic` (this lane) keeps the historical v1 experimentation context alive for revisits and adjacent work
- `ultrasonic-runtime-sync` is the forward push into the coordinated engine

Both lanes can carry work; the operator decides which one a given workstream belongs to based on whether it builds on v1 reasoning vs. forward-engine integration.

## Goal

Carry forward the v1 ultrasonic-measurement insights and apply them to specific scenarios that don't fit the coordinated-engine integration push — diagnostic tools, isolated experiments, alternative measurement schemes, edge-case calibration validation.

## In scope

- Standalone ultrasonic measurement experiments (not yet engine-integrated)
- Validation harnesses that compare ultrasonic vs. mic-driven vs. in-band-quiet-region drift detection
- Diagnostic tooling for ultrasonic burst design and bandwidth analysis
- Documentation and analysis of the original epic/03 experiments

## Out of scope

- Engine-integrated drift correction with bounded rate adjustment — that's `ultrasonic-runtime-sync`
- New mic-driven calibration features — `startup-mic`
- General audio engine durability — `feature-hardening`

## Discord

Lane Discord thread: see `discord_thread_bindings` for the current routing.

## Notes

- The historical `epic/03-runtime-ultrasonic-auto-alignment` branch is preserved as a reference.
- New work on this lane branches from `main`.
