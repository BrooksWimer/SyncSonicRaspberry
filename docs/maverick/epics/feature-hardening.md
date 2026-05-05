# Epic: feature-hardening

_The strategic context for hardening — North Star, three horizons, design principles — lives in [`../ROADMAP.md`](../ROADMAP.md), particularly §3.2 (H1-H8). Read that first. This file describes what counts as in-scope for this lane and what counts as someone else's lane._

## Goal

Take the working coordinated engine (deployed on the Pi, North Star reached) and make it survive contact with realities the operator's living room hasn't tested: degraded networks, controller drift, partial service crashes, USB hub contention, partial speaker failures, 24-hour soak under varied speaker mixes. Turn "works on this Pi" into "works on a stranger's Pi after a clean install."

## Why This Lane Exists

The North Star was reached on 2026-05-01 with a specific hardware configuration, three specific speakers, one specific room, and one operator who knows where every cable goes. Turning that into something a stranger can plug in and use means closing every silent failure mode that's currently masked by `Restart=on-failure`, by manual snapshot-rollback, by the operator knowing which speaker to power-cycle, and by the operator being the only person watching the journal.

## In Scope

- **H1 — Crash-safe service lifecycle.** `syncsonic.service` recovery story for BLE GATT registration failure, `pw_delay_filter` segfault, Icecast pipe sink dying mid-playback. Each path: documented behavior, journal log, automatic retry with backoff. Replace `RuntimeError`-on-first-failure code paths.
- **H2 — First-run + factory-reset UX.** Clean-install script (BlueZ config, audio group, virtual_out, services, USB-mic detection) that produces a verified-healthy node unattended. Factory reset: `bluetoothctl remove`, clear `control_state.json`, flush `~/.config/syncsonic`, restart service.
- **H3 — Telemetry retention + remote diagnostic export.** Log rotation for `~/syncsonic-telemetry/events/`. "Export last N hours" CLI producing a tarball with telemetry events, `journalctl -u syncsonic.service` for the same window, `pactl info`, `pw-cli list-objects`, BlueZ device list.
- **H4 — BLE protocol versioning.** Add `protocol_version` to `STATUS_RESPONSE` so a phone app drift produces a clear "update needed" banner instead of silent dropped notifications.
- **H5 — Latency-keep regression.** Per-speaker latency override source-of-truth: phone (AsyncStorage) vs Pi (`control_state.json`). Pick one, document it, add regression test.
- **H7 — Pi snapshot + rollback discipline.** Numbered snapshot scheme (`pre-<commit>-<timestamp>`), one-shot rollback script, rotation policy (currently snapshots accumulate forever).
- **H8 — Commercial-readiness checklist.** Living document of every assumption baked into the current Pi 4 deploy that would fail on a different SoC, distro, or audio stack version.
- 24-hour soak harness covering BT-only, BT+Sonos, transport-stress scenarios, and partial-speaker-failure recovery.

## Boundaries

- UI-side polish (button hierarchy, copy, telemetry visualization in the app) belongs in [`ui-polish`](ui-polish.md), not here. H6 from `ROADMAP.md` lives there.
- New audio features (runtime ultrasonic, in-band quiet-region correction) belong in [`ultrasonic-runtime-sync`](ultrasonic-runtime-sync.md).
- Hardware redesign (Pi CM5 carrier, soldered BT modules, enclosure, BOM) belongs in [`custom-hardware-design`](custom-hardware-design.md).
- Spatial / multi-room features belong in [`spatial-audio-awareness`](spatial-audio-awareness.md).

## Planning Guidance

- Pi-validation evidence is non-negotiable for anything in this lane. Local `compileall` + lint is necessary but never sufficient.
- Prefer narrow, targeted slices over big rewrites. The coordinated engine is working — don't refactor it; instrument it, recover from its failures, document its assumptions.
- A slice is "done" when it survives a 24-hour soak with the relevant scenario, not when the unit test passes.
- Snapshot-before-deploy is mandatory for any change touching backend code on the Pi.
- If a hardening item requires changing the audio engine itself, stop and ask whether it's actually a `ultrasonic-runtime-sync` or `feature-hardening` task. The engine is on `main` for a reason.
