# Epic: spatial-audio-awareness

> **Status change — 2026-06-04 (operator, Brooks).** This lane is **no longer low-priority
> scratch-work.** It is now SyncSonic's **North Star evolution**: replace the timing-only
> startup tune with a true **spatial tune**, and let the proven ultrasonic runtime loop
> *maintain* that spatial tune over time. The "much more complicated / not necessary for MVP"
> framing in `ROADMAP.md` §3.4 is **superseded** by this charter. (Roadmap §3.4 to be
> reworded in a follow-up; this doc is authoritative for the lane until then.)

## North Star

**SyncSonic is an inference engine for sound.** It builds a model of the listening
environment from cheap signals — speaker positions, the listener's seat, room geometry,
and *what each speaker is actually capable of* — and then drives a **heterogeneous array of
wireless speakers** (a Sonos soundbar + a cheap BT box + a Vizio, all at once) to deliver an
**enhanced, intentional listening experience**, not merely a synchronized one.

The leap this epic delivers:

- **From:** every speaker plays the same stereo mix, driven to a common latency target so
  they *emit* in sync (commodity multi-speaker sync).
- **To:** each speaker gets a role + delay + gain (+ later EQ / channel) computed from the
  *inferred environment model*, so the array behaves like one **designed** system tuned for
  the seat (the differentiator).
- **Maintained by:** the existing ultrasonic burst-detection / drift-correction loop, which
  already keeps speakers at a timing target — repurposed to keep them at the **spatial**
  target. The startup tune sets the baseline; the runtime loop maintains it.

## The constraint that shapes everything (and is also the moat)

SyncSonic's speakers are **wireless boxes behind a lossy codec.** This is the same reason
cross-correlation failed in the ultrasonic slice 0 (`proposals/06-ultrasonic-vs-inband.md`):
the codec does not preserve waveform fidelity. Consequences:

- **Full FIR room correction (CamillaDSP convolution, DRC-FIR, REW-style impulse inversion)
  is unreliable through our chain** — it needs impulse-response fidelity we don't have, plus
  thousands of taps and latency. Do **not** build the epic on it. (A cheap *experiment* to
  confirm exactly where the codec wall sits is in scope — see Track E.)
- **What survives the codec is the energy / level / timing domain** — exactly what the
  ultrasonic energy-detection already proved robust: **per-speaker delay, per-speaker level,
  coarse parametric/IIR EQ (broad tilt + a few bands), routing/crossover, and upmix.**

The moat: nobody does good **auto-spatial-tuning for arbitrary, heterogeneous wireless
multi-speaker setups.** Sonos Trueplay works only inside Sonos's closed ecosystem with its
own mic. SyncSonic groups *whatever speakers you own.* We win by leaning on what we can
**infer cheaply (geometry + capability)**, not on what we **struggle to measure (impulse
response)**.

## Three-layer model

| Layer | Job | Easy win → envelope-push |
|---|---|---|
| **INFER** | Build the environment model | manual entry → **ultrasonic seat-ranging** → **phone-camera room-mapping** |
| **TUNE** | Act on the model | seat coherence (delay+gain) → **heterogeneous roles / bass-mgmt** → upmix / envelopment |
| **MAINTAIN** | Hold the tune over time | the existing ultrasonic runtime drift loop (already proven) |

### Two leverage unlocks

1. **The ultrasonic system is already a ranging sensor.** Burst time-of-arrival measures
   latency today; with the mic at the *seat*, those same arrival times *are* the per-speaker
   distances/angles to the seat — the exact input the coherence tune needs. Auto-sensed
   geometry, reusing machinery we already trust.
2. **Heterogeneous speakers are the best demo, not the hardest problem.** Characterize each
   speaker with the chirp we *already emit* (`backend/measurement/startup_tune.py`) — usable
   frequency range, roll-off, max clean level. That's **band-energy = codec-robust** (unlike
   impulse correction). Then assign roles: bass to the speaker that can do bass (soundbar),
   cheap speaker to mids/fill or surround, crossover + level-match. The A/B is dramatic.

## Verification doctrine (per [[verification-centric-orchestration]])

This lane is exploratory, so **every track ships with its audible A/B defined BEFORE building.**
The bar is **Brooks can HEAR a real improvement vs the timing-only tune** — never "the numbers
say so." A track is "done" on a deliberate go / no-go with documented reasoning; "promising,
defer" is a valid outcome. Durable conclusions go to `PROJECT_MEMORY.md`, evidence to
`proposals/spatial-*.md`.

## Tracks (segmented for concurrency — see sequencing below)

- **Track A — Geometry inference (shared sensor).**
  - *A1 (easy win):* ultrasonic seat-ranging → per-speaker distance/angle, reusing burst ToA.
  - *A2 (envelope-push, research):* **phone-camera room-mapping** — detect speaker positions,
    the couch/listener, rough room geometry from phone photos/video → infer the spatial layout
    with zero manual input. Headline "inferred data" bet.
- **Track B — Coherence tune (align to the seat).** Foundation. Codec-robust. Consumes Track A
  geometry; reuses the live `delay_ms` + `left_percent`/`right_percent` knobs. *A/B: centered
  mono image tightens/centers vs the timing tune.*
- **Track C — Capability-aware heterogeneous tune (FLAGSHIP first win).** Characterize each
  speaker, assign roles, crossover + bass management. *A/B: a mismatched soundbar+cheap pair
  sounds intentionally designed; bass comes alive.* Highest wow, lowest codec risk.
- **Track D — Envelopment / upmix (experimental).** Distribute content across the array for
  immersion. Fuzzier A/B ("do you feel inside the sound?"); later.
- **Track E — Correction probe (background, cheap).** A small experiment: how much does coarse
  per-speaker IIR EQ actually help through our chain? Settles where the codec wall is so we
  *know* rather than assume. Produces a doc + go/no-go, not engine code.
  - **2026-06-17 result (done):** See [`proposals/spatial-eq-per-speaker-experiment.md`](proposals/spatial-eq-per-speaker-experiment.md). Actuation **go** (measure → infer → apply → combined validate). Full inverse flattening policy **no-go** for listening quality — noticeable but not preferred. All four levers (timing, volume, stereo L/R, EQ) confirmed available; next work is coordinated policy, not new infrastructure.

### Shared substrate

**CamillaDSP** runs on Pi 4 + PipeWire and natively expresses delay, gain, crossover, IIR EQ,
and mixing in one engine. Evaluate it as the actuation substrate for Tracks B/C/D so the
experiments compound instead of each hand-rolling actuation (today's path is the bespoke
`tools/pw_delay_filter.c` + `control_state.json`). Substrate choice is itself a Track-C
sub-decision, not a separate lane.

## Concurrency-safe sequencing (matches `WORKSTREAM_MODEL.md`)

Each workstream branches from `main`, attaches to this lane, finishes into the
`spatial-audio-awareness` branch. Concurrent workstreams only collide if they **edit the same
files**, so the plan assigns **disjoint file ownership** and sequences the one shared contract:

**Phase 1 — two workstreams in parallel, zero file overlap:**
- **WS-1 (flagship, code) — Track C with manual geometry.** Establishes the shared contracts
  (`EnvironmentModel`, `SpatialTunePlan`) + speaker characterization + role/crossover planner
  + actuation. Owns the new planner/engine modules. **Delivers the first audible win.**
- **WS-2 (research, parallel) — Track A2 camera feasibility.** Standalone prototype +
  `proposals/spatial-camera-mapping.md`. **No engine changes** → disjoint files → safe to run
  alongside WS-1.

**Phase 2 — fan-out after WS-1 merges (so the contract is stable on `main`):**
- **WS-3 — Track A1 ultrasonic seat-ranging** → writes geometry into `EnvironmentModel`.
- **WS-4 — wire camera output (from WS-2) into `EnvironmentModel`.**
- These branch from the updated `main`, own disjoint sensor modules, both populate the *same
  stable schema field* — concurrent and conflict-free.

Rule: if a proposed slice would edit files another in-flight slice owns, **split or sequence it**
(per `WORKSTREAM_MODEL.md`). Never two concurrent workstreams in the same module.

## First slice spec — WS-1 (flagship): capability-aware heterogeneous bass-management tune

**What we build.** A startup "spatial tune" that, for a set of connected speakers:
1. **Characterizes** each speaker by replaying the existing chirp and measuring per-band
   energy at the mic → a capability profile (usable low-end roll-off, relative max level).
2. **Assigns roles** from capability + (manually entered, for this slice) rough geometry:
   the most-capable speaker carries bass / acts as anchor; weaker speakers get a high-pass
   crossover and serve mids/fill.
3. **Emits a `SpatialTunePlan`** (per-speaker delay_ms, gain, crossover/role) into the live
   control plane (`control_state.json` knobs that already exist; crossover via the chosen
   substrate).

**Where we are now.** Timing tune drives `delay_ms` to a common latency target; per-speaker
`delay_ms` + `left_percent`/`right_percent` are already live; no capability profiling, roles,
crossover, or bass management exist yet.

**How Brooks verifies (audible A/B — defined before build).**
- Setup: one capable speaker (e.g. Sonos soundbar) + one weak cheap BT speaker, playing
  full-range music with real bass.
- **Mode A** = today's tune: both play the full stereo mix → the cheap speaker distorts/rattles
  on bass, the blend sounds muddy and accidental.
- **Mode B** = spatial tune: bass routed to the capable speaker, cheap speaker high-passed and
  level-matched → the pair sounds *intentional*; bass is clean and full; the cheap speaker
  stops straining.
- **Pass = Brooks clearly hears B as the better, "designed" sound.** If the difference is
  marginal, that's an honest finding about how much role-assignment buys us.

**Pi reality (operator-confirmed 2026-06-04).** Phone camera for room-mapping (Track A2). For
seat-ranging and listening tests, **the Pi is physically moved to the listening spot** so the
USB mic is at the seat; using the **phone mic** for this is a future experiment worth running.

## Carried-forward scope (still valid, now sequenced under the tracks above)

- Per-speaker channel routing (front-left / rear-right …) → Track C/D.
- Mic-driven room geometry via ToA → Track A1.
- Listener-position awareness → Track A (seat is the reference point).
- Atmos / surround decode into N speakers → Track D (big lift, later).
- HDMI ARC/eARC TV input + lip-sync → adjacent product, still out of this lane.

## Boundaries (unchanged)

- Not a multi-room (different zones, different songs) epic — that's a separate product.
- Engine-level changes follow doctrine: workstream from `main`, attached here, finished into
  the lane branch, then explicitly promoted. Don't add spatial infra to `main` ad hoc.
- Use Raspberry Pi validation for any claim about audio routing, latency, level, or
  end-to-end alignment — never local syntax-only verification (`reference_syncsonic_pi`).
