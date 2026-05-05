# SyncSonic Project Roadmap

The strategic horizon is in [`ROADMAP.md`](ROADMAP.md). This file is the Maverick-canonical view: six durable epics, each with a milestone definition, success signal, and link to its charter. Used by the Maverick planning agent to ground each slice in the right lane.

The North Star is reached. From here, the work is hardening the coordinated engine for deployable product use and exploring optional advancement lanes.

## Active milestones

### M1 — Feature hardening

**Why.** The coordinated engine works in the operator's living room with one specific hardware configuration. Industry-readiness means it survives degraded networks, controller drift, service crashes, USB hub contention, partial speaker failures, and 24-hour soak tests on diverse speaker mixes.

**Scope.** Stress scenarios + recovery behavior + boundary discovery. Crash-safe service lifecycle, first-run + factory-reset UX, telemetry retention + remote diagnostic export, BLE protocol versioning, latency-keep regression, snapshot/rollback discipline, commercial-readiness checklist. Maps roughly to ROADMAP §3.2 H1-H8.

**Success signal.** A clean Pi boot from a blank install reaches "verified-healthy node" in under 5 minutes. The system survives a 24-hour soak with mixed BT+Sonos under transport stress without operator intervention. A `support` CLI produces a debug bundle anyone could send the maintainer.

**Lane.** [`feature-hardening`](epics/feature-hardening.md) → branches from `main`.

### M2 — UI polish

**Why.** The Expo/React Native surface today reflects what the audio engine demanded — not what end users need. It needs a richer card system, telemetry visualization, status pills, sequence-progress feedback, and a deliberate visual hierarchy before it can land in a stranger's hands.

**Scope.** `SpeakerConfigScreen.tsx` audit, BT/Wi-Fi grouping in the speaker list, per-speaker telemetry visualizer (RSSI dip, current vs target latency, recent soft-mute events), animation cleanup, copy and theming.

**Success signal.** A non-technical user can pair speakers, run calibration, and read the status without operator coaching.

**Lane.** [`ui-polish`](epics/ui-polish.md) → branches from `main`.

### M3 — Custom hardware design

**Why.** Selling SyncSonic as a product means producing a sellable form factor — not a Pi 4 + USB hub on a desk. Enclosure, antenna placement, BT module integration, BOM, and FCC/CE pre-scans are all unstarted.

**Scope.** Pi CM5 carrier exploration, soldered BT module options vs USB, enclosure thermals, BOM costing, antenna placement, FCC/CE pre-scan checklist. Mostly research and design — not Pi-deploying. Decision gate: stay CM-class or jump to a true embedded SoC.

**Success signal.** A reference design (carrier + enclosure + BOM) that's been at least one design-review pass with someone who has manufactured PCBA hardware before.

**Lane.** [`custom-hardware-design`](epics/custom-hardware-design.md) → branches from `main`.

### M4 — Patent application

**Why.** The coordinated engine + bounded rate adjustment + soft-mute re-entry stack is genuinely novel for off-brand speaker synchronization. A patent application protects the IP before any commercial conversation; the operator already has prior research and architecture docs to build from.

**Scope.** Inventorship inventory (which mechanisms are claimable), prior art search, claims drafting, supporting documentation pack from existing `proposals/` and Pi validation evidence.

**Success signal.** A filed application (or a deliberate "we won't file" decision with documented reasoning).

**Lane.** [`patent-application`](epics/patent-application.md) → notes-and-prose lane, no transport changes; branches from `main`.

### M5 — Ultrasonic runtime sync

**Why.** Today's calibration is a discrete event triggered by a button press. BT speakers drift on the order of 20–80 ppm against each other, accumulating ~50 ms of relative offset over an hour. Continuous correction during music — without operator intervention — would close the loop. The operator has prior ultrasonic experiments in old branches that proved the concept; the new work is porting that into the coordinated engine and validating on the Pi.

**Scope.** Ultrasonic burst generator that integrates with the playback graph (psychoacoustic masking + speaker bandwidth gating), runtime measurement that doesn't require muting, bounded runtime correction (cap ±50 ppm rate adjustment), UX switch + "drift correction: on" status pill in `SpeakerConfigScreen.tsx`. Open question still active: ultrasonic vs in-band/quiet-region — small experiment before committing.

**Success signal.** A system that holds 3-speaker alignment within audible threshold over a 1-hour music session without operator intervention.

**Lane.** [`ultrasonic-runtime-sync`](epics/ultrasonic-runtime-sync.md) → branches from `main`. Architecturally downstream of the coordinated engine (already shipped on `main`).

### M6 — Spatial audio awareness

**Why.** Optional, exploratory lane. Per-speaker channel routing (front-left, rear-right), microphone-driven room mapping, listener-position-aware audio adjustment, eventually Atmos/Dolby decoding. The operator flagged this as "much more complicated and not at all necessary for MVP" — included as a durable lane so research scratch work has a home, not so it competes with M1-M5 for time.

**Scope.** Open. Begins with a feasibility study and a few mic-driven room geometry experiments. Bounded by the design principles in `ROADMAP.md` §4.

**Success signal.** Either (a) a credible plan with an estimated timeline, or (b) a deliberate "park indefinitely" decision with documented reasoning.

**Lane.** [`spatial-audio-awareness`](epics/spatial-audio-awareness.md) → branches from `main`. Lowest priority of the six lanes.

## Priority order

1. **M1 feature-hardening** — required before any external user touches the system.
2. **M2 ui-polish** — required for any external user to make sense of it.
3. **M5 ultrasonic-runtime-sync** — highest-value remaining audio-stack feature, blocked only by deliberate vs ultrasonic-vs-quiet-region decision.
4. **M3 custom-hardware-design** — required for commercial intent; not for personal/operator use.
5. **M4 patent-application** — required before any commercial conversation; can run in parallel with M1/M2 since it's notes-and-prose.
6. **M6 spatial-audio-awareness** — exploratory, deferred indefinitely unless a specific use case forces it.

## What was M1-M5 from the prior plan (now historical)

The old `epic/01..05` lanes are preserved in git as historical references; the new lane structure subsumes them:

- **Old epic/01 PipeWire transport stability** → folded into the coordinated engine (now on `main`); future stability work goes in `feature-hardening`.
- **Old epic/02 Startup mic auto-alignment** → done 2026-05-01; chirp + music anchor land in the coordinated engine.
- **Old epic/03 Runtime ultrasonic auto-alignment** → renamed and reframed as `ultrasonic-runtime-sync` (M5 above).
- **Old epic/04 Wi-Fi speakers manual alignment** → done 2026-05-01; Sonos is now an auto-aligned peer through the coordinated engine.
- **Old epic/05 Coordinated engine** → done 2026-05-05; rolled into `main` as the production substrate.

## See also

- [`ROADMAP.md`](ROADMAP.md) — long-form strategic doc, North Star, three-horizon plan, design principles, open questions carried forward.
- [`WORKSTREAM_MODEL.md`](WORKSTREAM_MODEL.md) — historical workstream conventions; the doctrine in this file supersedes the foundation/neutral-minimal model.
- [`PROJECT_MEMORY.md`](PROJECT_MEMORY.md) — durable decisions and the 2026-05-05 transition history.
