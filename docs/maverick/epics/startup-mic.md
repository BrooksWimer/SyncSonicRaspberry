# Epic: startup-mic

A first-class lane for ongoing startup-mic auto-alignment work.

## Status

v1 of startup mic auto-alignment was implemented and is now part of the coordinated engine on `main` (the chirp + music-anchor calibration that runs on every fresh playback round). That is **the floor**, not the ceiling — this lane owns ongoing improvements to mic-driven calibration.

The original epic/02-startup-mic-auto-alignment branch (now historical) preserves the v1 development trail.

## Goal

Keep the mic-driven calibration accurate, fast, and robust across the full set of conditions the engine ships under:

- Different mic models and USB audio interfaces (the current Pi-side rig uses one specific USB measurement mic; alternates need validation)
- Edge cases in the chirp / music-anchor cross-correlation when room acoustics are unusually hostile
- Multi-mic scenarios (operator may eventually want stereo-pair mics for room-mapping work)
- Re-calibration triggers — when does the engine decide to re-anchor without operator intervention

## In scope

- Mic hardware compatibility validation
- Alignment algorithm improvements (better peak-picking, better noise rejection, better envelope detection)
- Re-calibration policy refinement (latency drift threshold tuning, soft-mute coordination)
- Diagnostic tooling for mic input quality (level meters, SNR estimation, drift over time)

## Out of scope

- Spatial / multi-room work (`spatial-audio-awareness`)
- Runtime drift correction without re-calibrating (`ultrasonic-runtime-sync`, `runtime-ultrasonic`)
- General-purpose audio engine work (lives in the coordinated engine on `main`)

## Discord

Lane Discord thread: see `discord_thread_bindings` for the current routing.

## Notes

- The historical `epic/02-startup-mic-auto-alignment` branch is preserved as a reference.
- New work on this lane branches from `main`.
