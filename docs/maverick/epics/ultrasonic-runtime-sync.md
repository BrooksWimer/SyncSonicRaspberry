# Epic: ultrasonic-runtime-sync

_The strategic context — why runtime correction matters, why ultrasonic vs in-band is the open question, what bounded rate adjustment means — lives in [`../ROADMAP.md`](../ROADMAP.md) §3.3. The architecture this epic builds on top of is in [`../proposals/05-coordinated-engine-architecture.md`](../proposals/05-coordinated-engine-architecture.md). Read both before drafting a slice._

## Goal

Close the loop between speakers continuously while music plays. Today's calibration is a discrete event triggered by a button press; this epic adds inaudible runtime correction that absorbs the 20–80 ppm BT clock drift before it becomes audible — without the operator pressing anything.

## Why This Lane Exists

The coordinated engine ships with two Slice 2 + Slice 4 capabilities that almost solve runtime correction: an elastic delay engine that accepts smooth `set_rate_ppm` adjustments (no graph xrun), and a cross-correlation analyzer that can measure inter-speaker offset from a mic capture. What's missing is a continuous loop that emits an inaudible probe signal, measures its lag-per-speaker, and feeds bounded rate adjustments back to the elastic engine — all while music is playing.

The operator has prior ultrasonic experiments on old branches that proved the burst + measurement concept works on at least some BT speakers. Porting that into the coordinated engine and validating it at the system level is the bulk of this epic's work. The remaining open design question is whether ultrasonic (>= 18 kHz inaudible bursts) or in-band (chirps inserted into musically-quiet regions) is the better approach for the speaker mix the operator targets.

## In Scope

- **Open-question experiment.** A small slice that runs both ultrasonic-burst and chirp-in-quiet-region against the operator's three-speaker setup and produces measurement data + a recommendation. Don't commit to one until this is done.
- **Ultrasonic burst generator.** Integrates with the playback graph WITHOUT degrading listening: psychoacoustic masking, speaker bandwidth gating (some speakers brick-wall filter at 16 kHz), and emission cadence (1 Hz feels right, but verify).
- **Runtime measurement path.** Reuse the Slice 2 ring buffer + Slice 4 cross-correlation analyzer. Measurement that doesn't require muting other speakers.
- **Bounded runtime correction.** Cap at ±50 ppm rate adjustment per the `ROADMAP.md` §4 design principle. Never jump filter delay during music. The elastic engine accepts these adjustments smoothly — verify under load.
- **UX surface.** A switch to enable/disable in `SpeakerConfigScreen.tsx`, a "drift correction: on" status pill, and a small visualizer for current correction magnitude per speaker (coordinated with [`ui-polish`](ui-polish.md)).
- **24-hour soak validation.** A music session under varied speaker mixes that holds alignment within audible threshold without operator intervention. Pi validation evidence required.

## Boundaries

- Hardening of the existing engine (crash safety, logging, recovery) is `feature-hardening`, not here.
- BLE protocol versioning + status notifications are `feature-hardening` H4. This epic only adds notifications it specifically needs (e.g. correction magnitude per speaker).
- The actual visual design of the UX surface is `ui-polish`. This epic owns the data path; coordinate the visual.
- Any changes to the engine interfaces (new Unix socket commands, new IPC fields) need to land in the engine on `main` first, then this epic builds on top.

## Planning Guidance

- Architecturally downstream of the coordinated engine on `main`. Branches from `main`. Don't reintroduce the `foundation/neutral-minimal` lineage.
- The open question (ultrasonic vs in-band) is the first slice. Don't write the runtime loop until that decision is data-backed.
- Each slice ends with Pi validation evidence: a journal excerpt, a measurement plot, a soak session report. This is non-negotiable per `AGENTS.md`.
- Pet sensitivity to ultrasonic is a real risk for some operators. Document the experiment and fallback to in-band if needed.
- When the loop is shipping, update [`PROJECT_ROADMAP.md`](../PROJECT_ROADMAP.md) M5 status and append a "done" entry to [`PROJECT_MEMORY.md`](../PROJECT_MEMORY.md).
