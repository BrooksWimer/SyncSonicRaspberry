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

## Slice 0 Findings (2026-05-19) — open question resolved

**Ultrasonic wins on viability; cross-correlation is the wrong detector for it.**

Pi-validated on `syncsonic@10.0.0.89` against the operator's worst-case BT speaker (cheap Chinese unit). Full experimental record + raw metrics in [`../proposals/06-ultrasonic-vs-inband.md`](../proposals/06-ultrasonic-vs-inband.md). Three findings that determine slice-1 design:

1. **Ultrasonic survives the BT round trip with ~40 dB SNR.** Probes at 18.0–19.5 kHz (chirp) and 18.5 kHz (pure tone) reach the USB measurement mic with measurable energy in the 17.5–20 kHz band; ordinary music has near-zero content in that band so detection is naturally immune to music masking.
2. **The existing `analyze_lag.estimate_lag_samples` cross-correlation analyzer fails on ultrasonic over BT.** All three runs scored well below the analyzer's own `confidence_primary > 5` / `confidence_secondary > 2` "usable" thresholds; `peak_correlation` was essentially zero (-0.011 to +0.0031) and the chirp lag landed at the search-window boundary (-49 ms, physically impossible). Root cause: A2DP codecs (SBC / AAC / aptX) are psychoacoustically tuned to aggressively quantize >16 kHz content, preserving total energy but destroying waveform phase/shape.
3. **Direct spectral energy detection works for coarse tracking.** Sliding-window FFT in the 17.5–20 kHz band shows a clean ~40 dB step at chirp arrival (t = 0.85 s) and a corresponding step at chirp end. Slice 3 Pi validation later showed the 50 ms window / 25 ms hop detector is not precise enough by itself for +/-10 ms runtime correction; the next measurement slice must use exact filter emit frames plus mic sample-index / pattern matching before correction is promoted.

**Slice 1 architecture (data-backed, not speculative):**

- Probe shape: **short ultrasonic bursts on a known cadence** (operator suggestion validated by the experiment — cadence + envelope detection sidesteps the codec-mangling problem because energy preservation is enough)
- Burst frequencies: **rotate across 18.0 / 18.5 / 19.0 / 19.5 kHz** per cadence cycle for disambiguation and per-frequency speaker-response measurement
- Detector: a new `analyze_envelope.py` alongside `analyze_lag.py` — bandpass 17–20 kHz, envelope follower, peak detector. Do **not** reuse the existing `lag_analyzer` for runtime; keep it for startup calibration only.
- Drift correction path: unchanged — feed measured per-speaker lag into the Slice 2 elastic engine's `set_rate_ppm` socket with the ±50 ppm cap from `ROADMAP.md` §4.

The **Open Design Questions** section above is closed for slice 1 planning. The "Ultrasonic vs in-band chirp" question is resolved in favor of ultrasonic; the "Measurement cadence" question still needs CPU-load validation but the 1 Hz hint from the charter is consistent with the slice-0 burst-duration evidence.

## Slice 1 emission strategy (revised 2026-05-25)

The slice-0 architecture description above remains correct for the **detector** half (envelope follower + bandpass + peak detector). The **emission** half was implemented in `backend/syncsonic_ble/helpers/arrival_burst_actuation.py` as `paplay --device=bluez_output.<mac>.1` direct-to-BlueZ-sink; Pi validation 2026-05-25 showed that approach (a) bypasses the `pw_delay_filter` so end-to-end timing can't be measured, and (b) causes audibly choppy music due to two streams competing at the BT sink. See [`../PROJECT_MEMORY.md`](../PROJECT_MEMORY.md) 2026-05-25 entry for the full evidence.

The corrected emission design — call it **Option C** — moves burst generation *into the per-speaker `pw_delay_filter` process itself*. The filter already sits in the music path between `virtual_out` and the BlueZ sink, already has frame-precise output-clock visibility, and already exposes a Unix socket control surface (`/tmp/syncsonic-engine/syncsonic-delay-<mac>.sock`). Slice 1 extends that surface so the operator (and eventually the drift loop) can request a burst and read back the exact output-frame index at which it left the filter.

What this changes for slice 1 scope:

- The burst generator moves from Python (`probe_signals.build_runtime_ultrasonic_burst`) into the C filter, OR Python pre-renders a sample buffer that the filter mixes into its output ring at a requested frame boundary. Decision: planning agent to resolve based on filter performance constraints.
- The filter's wire protocol grows two commands: `emit_burst` (with frequency / duration / amplitude args) and `query_emit_timestamps` (event stream of `frame_index_emitted` per burst). The existing `set_delay` socket can be reused.
- `arrival_burst_actuation.py`'s job becomes "issue the socket commands and forward the resulting timestamps to the detector" — much smaller than the current 232-line direct-`paplay` orchestrator.
- The detector (`analyze_envelope.py`, not yet written) consumes the per-burst `frame_index_emitted` (converted to capture-clock time) plus the mic capture, and reports per-speaker latency.
- The drift-correction loop is unchanged from the slice-0-findings description: per-speaker measured latency → bounded `set_rate_ppm` to the elastic engine.

**Out of scope for the new slice 1:**

- The detector (`analyze_envelope.py`) — separate slice. Slice 1 ships the emission infrastructure and a manual validation harness; the live detector and drift loop follow.
- Multi-speaker simultaneous emission — single-speaker first; cadence-coordination across speakers comes after the single-speaker path works.

**Validated boundaries from the slice-0 + 2026-05-25 evidence:**

- Burst at 18.5 kHz with raised-cosine fades produces ~50 dB SNR at the mic on heyday — well above any detection threshold
- Music masking is not a concern (band is empty of music content)
- BT codec latency on the test speaker is ~370 ms with ±25 ms variance; the detector's window doesn't need to be tight
- Filter-resident emission must not introduce its own xruns under the engine's existing load; CPU budget is a real constraint and should be measured as part of slice 1 verification

The slice 1 v1 implementation on `maverick/syncsonic/ultrasonic/slice-1-cadence-based-ultrasonic-envelope-detector-drift-correction-loop-9437d092` (commit `02b581b`) is preserved as a reference for the actuation-manager integration pattern (`ActuationManager.set_manual_delay`, BLE handler shape, scheduling primitives) — the wiring there is fine, only the emission target is wrong.

### Slice 1 Option C implementation note (2026-05-25 Pi validation)

The revised workstream branch `maverick/syncsonic/ultrasonic/slice-1-revised-in-filter-ultrasonic-burst-emission-4307e4eb` implements filter-resident burst emission by adding `emit_burst` and `query_emit_timestamps` to `pw_delay_filter`'s Unix socket protocol. The C filter now queues one burst request at a time, schedules it after the current delay depth, mixes a raised-cosine 18.5 kHz sine into both output channels, and logs the first emitted output frame. Python actuation is now a thin socket-command wrapper; the retired direct-`paplay` path is not used.

Pi validation on heyday (`45:7A:D9:00:81:19`) used the live baseline delay instead of disturbing music alignment. Baseline query returned `target_delay_samples=5424` (`113.0 ms`). A validation script compared per-burst scheduling offsets relative to `frames_out_total` immediately before each `emit_burst`: baseline offset was `5424` samples; baseline+500 ms (`613.0 ms`, `29424` samples) offset was `29424` samples; delta was exactly `24000` samples. The script restored heyday to `target_delay_samples=5424`, and a follow-up query confirmed `current_delay_samples_x100=542400`.

Deployment note: `start_syncsonic.sh` also needs `-lm` on its auto-rebuild command. Updating only the Python transport compile command is insufficient because systemd startup rebuilds `tools/pw_delay_filter` directly when the C source is newer.

## Path to full-time runtime alignment (roadmap, 2026-05-26)

Slice 0 + slice 1 delivered the foundation: probe choice (ultrasonic + envelope detection) and filter-resident burst emission with frame-precise emit timestamps. **Three more slices reach "alignment is a constant part of the application running continuously while music plays."**

### Slice 2 — open-loop latency measurement (dispatched 2026-05-26)

Build the per-burst latency measurement subsystem. Single-direction: emission → mic → detector → log. No feedback into the elastic engine.

**Scope:**
- Python service on Pi that runs continuously when started
- Captures mic stream from USB measurement source via PipeWire
- Cycles through active BT speakers at 15-second cadence per speaker (2-speaker scope for the operator's current setup)
- For each emit cycle: query the speaker's filter delay, issue `emit_burst`, capture the `frame_index_emitted` from `query_emit_timestamps`
- Detector runs sliding-window FFT (50 ms window, 25 ms hop, 17.5–20 kHz band) on the mic stream within a context-aware window centered on the expected arrival time (filter delay + estimated BT codec latency from prior measurements)
- Computes `latency_ms = arrival_time - emit_time` per burst using wall-clock alignment between the emit-return monotonic timestamp and the mic-chunk arrival monotonic timestamp
- Logs every emit, every arrival (or "missed burst"), the slider value at emit, and the computed latency to the systemd journal
- Operator inspects via `journalctl -u syncsonic.service -f` over SSH

**Validation:** 10-minute run with operator manually moving the filter-delay slider on each speaker. Journal must show that slider moves correlate with corresponding shifts in measured arrival. That's the proof end-to-end measurement works.

**Validated boundaries (assumed from slice 0 + 2026-05-25 evidence):**
- Burst is mic-detectable at >40 dB SNR through the BT path on each in-use speaker
- Music playing in parallel does not mask the ultrasonic burst (band is empty of music content)
- 15-second cadence is well outside any plausible BT codec latency (~370 ms on heyday), so bursts cannot overlap

### Slice 3 — closed-loop drift correction during music playback

Take the measurement stream from slice 2 and feed it into the elastic engine's `set_rate_ppm` to maintain alignment automatically while music plays.

**Scope (provisional, refine when slice 2 measurements are in hand):**
- Wire the slice 2 measurement service into the actuation manager / elastic engine
- Bounded correction: ±50 ppm cap per `ROADMAP.md` §4
- Confidence gating: only correct when N consecutive measurements per speaker agree within tolerance — never apply a correction based on a single noisy measurement
- Operator escape: a CLI / BLE command to disable correction immediately
- Auto-disable on detector confidence drop (missed bursts, SNR collapse, etc.) so correction silently stops rather than going wrong
- Probably switch from wall-clock to audio-clock alignment here since drift signal is at the µs/s scale where wall-clock noise matters

**Two-speaker scope** stays for slice 3. The architecture should not bake in "exactly two speakers" — but the validation runs and tuning happen with the operator's current setup.

After slice 3 lands and runs cleanly for at least one music session, **"runtime alignment is a constant part of the application" is true for the 2-speaker case**.

### Slice 4+ — hardening before epic promotion

Bringing the runtime loop from "works on the operator's setup" to "ready for epic → main promotion" requires:

- **Multi-speaker scaling beyond 2.** Investigate what was "off" with the third speaker. Add frequency rotation (18.0 / 18.5 / 19.0 / 19.5 kHz across speakers) if simultaneous emission becomes valuable for higher measurement rates.
- **UX surface.** Toggle in `SpeakerConfigScreen.tsx`, "drift correction: on" status pill, per-speaker correction magnitude visualizer. Coordinates with the `ui-polish` epic.
- **Soak validation.** 24-hour music session with varied speaker mixes under network/codec stress, holding alignment within audible threshold with zero operator intervention.

Slice 4+ may shake out into multiple discrete workstreams; sizing comes from what slice 3 surfaces.

### Epic promotion gate

The `ultrasonic-runtime-sync` epic branch promotes to `main` only after slice 4+'s soak validation passes. Until then the epic stays unmerged — operator continues to opt in to alignment via the slice 3 service start command.
