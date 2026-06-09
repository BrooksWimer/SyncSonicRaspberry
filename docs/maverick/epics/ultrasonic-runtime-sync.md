# Epic: ultrasonic-runtime-sync

_The strategic context — why runtime correction matters, why ultrasonic vs in-band is the open question, what bounded rate adjustment means — lives in [`../ROADMAP.md`](../ROADMAP.md) §3.3. The architecture this epic builds on top of is in [`../proposals/05-coordinated-engine-architecture.md`](../proposals/05-coordinated-engine-architecture.md). Read both before drafting a slice._

## Goal

Close the loop between speakers continuously while music plays. Today's calibration is a discrete event triggered by a button press; this epic adds inaudible runtime correction that absorbs the 20–80 ppm BT clock drift before it becomes audible — without the operator pressing anything.

## Status — Concluding default-aligner slice as of 2026-06-05

The concluding slice makes runtime ultrasonic alignment the default aligner. The permanent `runtime-latency.service` is responsible for both initial convergence and maintenance corrections; the audible startup tune remains available only as an explicit operator action in the frontend.

Pass criteria for this slice are operator-visible/listenable on the real speakers, not only unit-test metrics:

- Restarting the service converges speakers through ultrasonic runtime correction with no default audible chirp.
- A badly offset speaker around 600 ms receives a large confidence-gated `set_delay` correction instead of a `slice5_freak_outlier_skip`.
- Per-speaker auto-align participation can be toggled off and on; excluded speakers receive no burst emission, measurement, or correction while excluded, and their delay is left untouched.
- The app shows live per-speaker runtime correction feedback and excluded state.
- The audible alignment tune still works when explicitly selected.

## Status — Production-effective as of 2026-06-03

The runtime closed-loop ultrasonic alignment system is in **production-effective steady state**. After ~3 days of continuous operation (62 hours, 6,786 captured measurements on Pi 10.0.0.89) and live operator listening tests, the system holds 2-speaker BT alignment within ~15 ms one-sigma without operator intervention, generates zero audible artifacts in steady state, and self-recovers from amplitude-detection failures.

### What's shipping (architecture summary)

**Measurement primitive (slices 15, 18)** — single-burst ultrasonic emission at 18.5 kHz with sparse 30-second cadence per speaker. Time-of-arrival measured via cross-correlation against an analytically-synthesized envelope template, with quadratic sub-sample peak interpolation. Empirical precision: sub-millisecond at SNR >= 9 dB (slice 15 design target), measured median 0.4 ms across capture archive.

**Adaptive amplitude (slice 9 + slice 18 smart adjustment)** — per-speaker amplitude ladder `(300, 600, 950)` with **symmetric step-on-miss / step-on-success**. A miss escalates one rung, a success drops one rung. Steady-state operation settles at the lowest rung that maintains detection — amp 300 (the slice-4-validated inaudibility floor) on cooperative speakers, with brief escalation only when bursts genuinely miss.

**Confidence-gated actuation (slice 18.2)** — corrections are derived from the median of a sliding 5-measurement window per speaker, not from individual readings. Two thresholds must both be exceeded for a correction to fire: the absolute apply-threshold (30 ms) AND a noise-floor gate (2x window standard deviation). The window resets on every applied correction. The system therefore self-throttles during noisy stretches and only acts when the evidence is consistent across multiple measurements.

**Single-burst detection (slice 18)** — replaced the 3-burst-at-300ms-spacing pattern with a single isolated burst per cycle. This eliminated the ~100 ms detector artifacts visible in earlier 3-burst data, where the matcher could lock onto wrong bursts within the pattern or get confused by lingering room echoes. Empirical impact: 2C:FD cycle-to-cycle std improved from 42 ms to 15 ms (3x); jumps >100 ms dropped from 29% of cycles to 0.06%.

**Persistent target (slice 11)** — runtime reads `target_total_ms` from the same persistence file the BLE startup-tune writes, so runtime and calibration stay synchronized when the operator changes the target.

**Frontend transmission (slice 14)** — every applied correction surfaces to the React Native frontend via BLE `CALIBRATION_RESULT` with `phase='runtime_correction'`. Phone app animates the per-speaker slider in real time.

**Permanent systemd unit + startup gate (slice 12)** — runtime-latency lives at `/etc/systemd/system/runtime-latency.service`, auto-starts after `syncsonic.service` when speakers are connected, exits cleanly after 5-minute timeout if no speakers join.

**Raw-mic archive (slice 17)** — every burst arrival and miss is captured as a WAV file with JSON sidecar at `/var/lib/syncsonic/observe-raw/<session_id>/`, enabling any future detector improvement to be re-analyzed against the same physical signal. Already accumulated a 62-hour reference archive (650 MB / 7,277 captures).

### Empirical verification record

- **3-day soak (62.3 hours, 2 BT speakers, single-burst mode):** F4:6A median latency 499 ms, std 15.6 ms; 2C:FD median 502 ms, std 14.6 ms; cycle-to-cycle |delta| median ~9 ms.
- **Jump frequency:** ≥100 ms cycle-to-cycle jumps occur in 0.06–0.17% of cycles. Cross-speaker correlation analysis (0% coincidence vs 11% baseline) confirms jumps are independent per-speaker BT codec internal-buffer behavior, not common-mode pipeline issues.
- **Long-window drift:** essentially zero (≤0.2 ms/min). Real physical drift on these speakers is below the level our measurement can resolve cleanly.
- **Live confidence-gate test (2026-06-03, 15 minutes):** zero corrections fired while window standard deviation was high; operator confirmed bursts inaudible in steady state and system audibly transparent.
- **Operator framing 2026-06-03:** "we have a very strong fully fleshed out tool."

## Future exploration

The system is production-effective for the 2-speaker BT case at the operator's hardware. Areas where further work would improve the system but are explicitly not committed work-streams as of 2026-06-03:

- **Cross-correlation precision improvements / measurement protocol research.** Slice 15 brought single-measurement precision to sub-millisecond; further detector work (longer integration as a "stage-1 join measurement", template tuning, better noise estimation) could enable lower apply thresholds and faster drift response.
- **Frequency adaptation for bandwidth-limited BT speakers.** Some BT speakers brick-wall filter above ~16 kHz; F4:6A showed marginal SNR at 18.5 kHz on the operator's hardware. A fallback frequency ladder (drop to 17 / 16 kHz before resorting to audible chirp) would handle these without sticky-amp-950 audibility regressions.
- **Raw-mic-archive analysis tooling.** The slice-17 archive is sitting unused; building a small re-analysis workflow against it would let any future detector be validated retroactively without recapturing data.
- **24-hour soak validation.** The 62-hour single-burst-mode soak already approximates this; a formal 24-hour music session under varied acoustic conditions with structured pass/fail criteria remains as a polish item before formal epic promotion to main.
- **WiFi speaker PipeWire integration.** Current implementation only sees speakers that have a `pw_delay_filter` socket; WiFi (Sonos / AirPlay / RAOP) speakers are out-of-band and the closed-loop has nothing to correct. This is the single most architecturally significant gap for true heterogeneous-transport setups. Operator notes it is the only future-exploration item that might warrant a real workstream eventually, but no commitment as of 2026-06-03 — there is open design space about whether PipeWire integration is even the right approach versus a separate trust-the-protocol design for WiFi.
- **Adaptive measurement cadence.** Drift data confirms steady-state behavior is stable for long windows; slow cadence (60-120 sec) with fast cadence (5-10 sec) post-jump-detection would reduce burst emission rate and amp-ladder thrashing in normal operation.

These are not committed slices. They are recorded here so future planning sessions have empirical context for whether to pick them up.


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

## Roadmap

_Refreshed 2026-06-08. Epic promoted to `main` this date. Detailed slice prose for the older slices lives in the **Path to full-time runtime alignment** section below._

### Done

- **Slice 0** — Open-question experiment (ultrasonic vs in-band). Resolved: ultrasonic wins.
- **Slice 1** — Filter-resident ultrasonic burst emission. `pw_delay_filter` gained `emit_burst` + `query_emit_timestamps`. Pi-validated.
- **Slice 2** — Open-loop latency measurement service. Per-burst arrival measurement against active speakers, journal-logged.
- **Slice 3** — Pattern measurement + relative proposal generation. Computes what timing adjustments WOULD be applied, without actuating.
- **Slice 3 follow-up** — Measurement cleanup + pattern/relative-proposal CLI flags. (workstream `f16ede00`, 2026-05-30, software-only verified)
- **Slice 4** — Live observation of proposed alignment adjustments on Pi. (workstream `dfec41a7`, 2026-05-30, software-plus-pi verified)
- **Slice 5** — Closed-loop actuation, **ppm-only** (slider stage stripped after live testing exposed architectural mismatch — slider changes filter delay, residual is sample-clock-derived and invariant to filter-delay changes, so slider could not drive residual to zero). System ships with: bounded ±50 ppm `set_rate_ppm` corrections, per-speaker confidence gating, SIGUSR1 + `MAVERICK_CORRECTION_STOP` env var operator escape (<1 s response), opt-in via `--enable-correction`. (workstream `cbb33bdc`, 2026-05-30, software-plus-pi verified)
- **Slices 6–18** — Latency-baseline slider, stability fixes, adaptive amplitude ladder, cross-correlation-on-envelope precision upgrade (50–100× vs peak detector), observe-only soak mode, raw-mic capture archive, confidence-gated actuation (median-of-3 + 2σ floor). Full detail in measurement ledger at `docs/maverick/proposals/10-runtime-ultrasonic-measurement-ledger.md`.
- **Conclusionary slice** _(2026-06-08, operator-validated)_ — Promoted ultrasonic to the unconditional default aligner:
  - Removed `FREAK_THRESHOLD_MS` outlier guard; the confidence window (median-of-3 + σ gate) is now the sole input gate, enabling correction of large initial offsets without false outlier rejection.
  - Per-speaker opt-out toggle added to `SpeakerConfigScreen.tsx` (`SET_ULTRASONIC_PARTICIPATION` BLE opcode `0x6A`); exclusion state persisted in `/run/syncsonic/ultrasonic_excluded.json`.
  - `RuntimeCorrectionWatcher` daemon thread added to the GATT service (`syncsonic_ble/runtime_corrections.py`): tails `runtime_corrections.jsonl` at 250 ms intervals and forwards all phase=`runtime_correction` events as `CALIBRATION_RESULT` BLE notifications so the frontend autosync card and latency slider update in real time.
  - Fixed `"event"` vs `"phase"` field mismatch between actuator JSONL output and frontend parser.
  - All actuation states (`building_window`, `within_threshold`, `insufficient_confidence`, `corrected`) now written to JSONL, enabling full frontend state progression.
  - Measurement cadence tuned: `cadence_sec` 15 → 5, `CONFIDENCE_WINDOW_N` 5 → 3 (time to first correction ~36 s vs ~2.5 min). Pattern gap and sigma ratio left unchanged.
  - Stale `--slice4-observe` flag removed from `runtime-latency.service`.
  - 4 pre-existing stale backend tests greened.

### Promoted to main

**2026-06-08** — `ultrasonic-runtime-sync` merged to `main` by operator decision. The formal Slice 7 soak gate was overridden; the conclusionary validation session on `syncsonic@10.0.0.89` (2-BT-speaker setup, JBL + secondary, live music, frontend confirmed receiving BLE corrections) was accepted as sufficient for promotion. The post-correction settling race (identified during validation: short cadence + window reset produces transient measurements before BT codec pipeline settles → potential over-correction) is a known open issue documented in the `correction-hardening` planned workstream below.

### Follow-on workstream: correction-hardening

Not yet opened. Planned scope:
1. **Post-correction settling holdoff** — suppress actuation for N cycles after any correction fires, independent of `clock_prior_reset_remaining`. Fixes the settling race observed during conclusionary validation.
2. **Per-cycle correction magnitude cap** — hard ceiling (e.g. ±150 ms per firing) so a bad transient window cannot produce a large single-cycle mis-alignment.
3. **Adaptive per-sample input clamp** — per-speaker rolling mean ± k×σ replaces the hard `FREAK_THRESHOLD_MS`; rejects outliers without blocking legitimate large-offset corrections.
4. **Dynamic alignment target** — at session start compute `target = max(per-speaker baseline latencies) + safety_margin` rather than using a static stored value. Eliminates the 5000 ms target that lingers from prior Sonos sessions when only BT speakers are present; minimises total system latency while still achieving alignment.
5. **Convergence / tracking two-phase control** — wide-net fast-convergence phase on first connect, narrow-net slow-tracking phase once within threshold. Reduces time to initial alignment without sacrificing steady-state stability.

### Strategy decisions on record

- **Ultrasonic > in-band chirp.** Decided slice 0; burst detection survives codec mangling, in-band did not.
- **Envelope FFT detection, NOT cross-correlation.** Decided slice 0; A2DP codecs destroy phase/shape, preserve energy.
- **Filter-resident emission, NOT direct-to-BlueZ.** Decided slice 1 revision; direct `paplay` bypasses the delay filter and causes audible chop.
- **Bounded rate adjustment.** Per `ROADMAP.md` section 4 — never jump filter delay during music; cap at the documented limit.
- **Burst amplitude: amp_x1000=300 (0.30 linear).** Decided 2026-05-30 empirically via slice-4 amplitude sweep. Standalone bursts inaudible at amp 0.95; sweep showed no consistent audible codec interaction at 0.95/0.30/0.10/0.03. Chose 300 (one order of magnitude below original) as conservative slice-5 default with +41 dB detection headroom. Drop further if any audibility recurs in live test.
- **Slider stage is NOT conjugate to relative_residual_ms.** Decided 2026-05-30 empirically via slice-5 live testing. Moving filter delay shifts both the emit-frame and arrival-sample indices by the same amount, so `sample_clock_delta` (and therefore relative residual) is largely invariant to slider corrections. Slice 5 ships ppm-only as a result.
- **Two-speaker scope first.** Architecture must not bake in N=2, but tuning + validation done on the operator current setup.
- **Single-direction first, closed-loop second.** Measurement (slices 2/3) shipped before actuation (slice 5) so we can validate proposals against reality without risking audio.
- **Confidence window is the sole outlier gate (post-conclusionary).** Decided 2026-06-08; `FREAK_THRESHOLD_MS` removed. Median-of-3 + σ floor provides outlier resistance without blocking large legitimate offsets. Known risk: post-correction transient measurements can dominate the window when cadence is short — addressed in `correction-hardening` workstream.
- **Dynamic alignment target needed for BT-only setups.** Decided 2026-06-08; 5000 ms static target is legacy artifact of sessions with a Sonos present. Proper fix is `target = max(baselines) + margin`, planned for `correction-hardening`.

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
3. **Direct spectral energy detection works.** Sliding-window FFT in the 17.5–20 kHz band shows a clean ~40 dB step at chirp arrival (t = 0.85 s) and a corresponding step at chirp end. Time resolution of the envelope detector with 50 ms windows / 25 ms hop is sufficient to time burst arrivals well within the drift-correction precision the runtime loop needs.

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

## Progress log (auto)

- 2026-05-30 — Slice 3 follow-up measurement cleanup (f16ede00-2681-4720-8da1-66c5f8cb86dc): Operator-confirmed verification (software-only) passed. → docs/maverick/workstreams/f16ede00-2681-4720-8da1-66c5f8cb86dc.md
- 2026-05-30 — Slice 4: live observation of proposed alignment adjustments (dfec41a7-7681-47af-a78a-79352c1dcb2a): Operator-confirmed verification (software-plus-pi) passed. → docs/maverick/workstreams/dfec41a7-7681-47af-a78a-79352c1dcb2a.md
- 2026-05-30 — Slice 5: closed-loop actuation with two-stage control (cbb33bdc-862c-44a0-9e58-3f051d71ec5f): Operator-confirmed verification (software-plus-pi) passed. → docs/maverick/workstreams/cbb33bdc-862c-44a0-9e58-3f051d71ec5f.md
- 2026-05-30 — Slice 5: closed-loop actuation with two-stage control (cbb33bdc-862c-44a0-9e58-3f051d71ec5f): Operator-confirmed verification (software-plus-pi) passed. → docs/maverick/workstreams/cbb33bdc-862c-44a0-9e58-3f051d71ec5f.md
- 2026-05-31 — Slice 6: latency-baseline slider stage (f48cb5ac-310d-4fd5-a85c-000fe5e51835): Operator-confirmed verification (software-pi-and-listening) passed. → docs/maverick/workstreams/f48cb5ac-310d-4fd5-a85c-000fe5e51835.md
- 2026-05-31 — Slice 8: SUSPENDED recovery + windowed CONFIDENCE_DROP gating (4873278f-1365-47e9-953f-7985d17af66d): Operator-confirmed verification (software-plus-pi) passed. → docs/maverick/workstreams/4873278f-1365-47e9-953f-7985d17af66d.md
- 2026-05-31 — Slice 9: adaptive burst amplitude ladder for unmeasurable speakers (858d67ab-7910-4313-aed0-f7e8f886d4ec): Operator-confirmed verification (software-only) passed. → docs/maverick/workstreams/858d67ab-7910-4313-aed0-f7e8f886d4ec.md
- 2026-05-31 — Slice 11: read target_total_ms from last-calibrated value, not CLI default (83689aea-6a3b-463f-ad81-8980a6481407): Operator-confirmed verification (software-only) passed. → docs/maverick/workstreams/83689aea-6a3b-463f-ad81-8980a6481407.md
- 2026-05-31 — Slice 12: permanent runtime latency systemd unit (5392a936): Local compile/diff checks passed; pytest and Pi validation blocked by missing pytest and unreachable Pi. → docs/maverick/workstreams/5392a936-slice-12-permanent-runtime-latency-service.md
- 2026-05-31 — Slice 12: convert runtime-latency from transient systemd-run to permanent auto-starting unit (5392a936-a524-4480-a602-3e71d0d0be6b): Operator-confirmed verification (software-only) passed. → docs/maverick/workstreams/5392a936-a524-4480-a602-3e71d0d0be6b.md
- 2026-05-31 — Slice 14: surface runtime corrections to the React Native frontend via BLE (6e5641ba-ce7e-4b26-8744-e61520c1c690): Operator-confirmed verification (software-only) passed. → docs/maverick/workstreams/6e5641ba-ce7e-4b26-8744-e61520c1c690.md
- 2026-05-31 — Slice 14: surface runtime corrections to the React Native frontend via BLE (6e5641ba-ce7e-4b26-8744-e61520c1c690): Operator-confirmed verification (software-only) passed. → docs/maverick/workstreams/6e5641ba-ce7e-4b26-8744-e61520c1c690.md
- 2026-05-31 — Slice 15: cross-correlation-on-envelope measurement upgrade 50-100x precision (f6a4f213): In progress. → docs/maverick/workstreams/f6a4f213-slice-15-cross-correlation-on-envelope-measurement-upgrade.md
- 2026-05-31 — Slice 15: cross-correlation-on-envelope measurement upgrade (50-100x precision) (f6a4f213-852b-494f-a96e-8d2353e6611f): Operator-confirmed verification (software-only) passed. → docs/maverick/workstreams/f6a4f213-852b-494f-a96e-8d2353e6611f.md
- 2026-06-03 — Slice 18: production-effective measurement architecture (retrospective audit) (c6e68ccf-5e04-4ad4-b07d-7ed0efe34913): Operator-confirmed verification (software-only) passed. → docs/maverick/workstreams/c6e68ccf-5e04-4ad4-b07d-7ed0efe34913.md
- 2026-06-05 — Concluding ultrasonic default-aligner slice: removed the large-offset freak skip, added per-speaker ultrasonic participation control, demoted audible startup tune to explicit opt-in in the frontend, and surfaced live runtime correction status in the speaker list. Local verification plus operator Pi listening/visual protocol required before PR acceptance.
- 2026-06-09 — Correction hardening: diagnosed dynamic target startup inflation on the Pi. `dynamic_target_set` was driven by raw ~5000 ms baseline samples that included existing filter delay; runtime now stores baseline samples as `measured_latency_ms - current_filter_delay_ms`, preserving the target margin while avoiding stale slider-delay inflation. Local verification: `RESERVED_HCI=hci0 RESERVED_ADAPTER_MAC=00:00:00:00:00:00 python3 -m pytest` passed with 58 tests.
- 2026-06-09 — corrected ultrasonic alignment (65209d8b-f765-412a-b106-3270f741460a): Operator-confirmed verification (software-only) passed. → docs/maverick/workstreams/65209d8b-f765-412a-b106-3270f741460a.md
- 2026-06-09 — fast-align speed improvement (680c73d5-619b-442f-af5e-28a6d951b4b1): Operator-confirmed verification (software-only) passed. → docs/maverick/workstreams/680c73d5-619b-442f-af5e-28a6d951b4b1.md
