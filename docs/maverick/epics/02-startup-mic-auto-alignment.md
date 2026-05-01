# Epic 02: Startup Mic Auto-Alignment

> **Status: DONE (2026-05-01).** Both startup-tune (chirp) and music-based
> calibration buttons reach `applied` reliably across pure-BT and BT+Wi-Fi
> configurations. Wi-Fi speakers were folded into this epic via the anchor
> pattern (Sonos provides the alignment target, BT speakers are pulled UP
> to match). Real-world 3-speaker (2 BT + Sonos) alignment confirmed by
> the project owner as "perfectly aligned" 2026-05-01 EDT. Runtime
> evidence: architecture proposal §17 + §18.

## Goal

Add a startup-only microphone calibration flow that uses human-audible probe
sounds to measure end-to-end latency and publish clean alignment targets into
the stable actuation path.

## In Scope

- startup probe generation and capture
- latency estimation at boot or startup
- publishing startup targets through the shared control-plane/actuation layer
- calibration UX or operator guidance tied to startup alignment

## Out of Scope

- runtime ultrasonic correction while music is already playing
- Wi-Fi speaker feature work
- unrelated PipeWire transport experiments outside what startup calibration
  strictly needs

## Starting Point

Begin from `foundation/neutral-minimal`. Reintroduce only the startup mic
pieces needed for startup calibration, not the broader experimental stack.

## Validation Expectations

- local backend/frontend checks as applicable
- Raspberry Pi validation is mandatory
- evidence should show the startup flow can measure and apply alignment without
  destabilizing the baseline runtime

## Final Artifacts (2026-05-01)

Code shipped on `epic/05-coordinated-engine`, latest landing commit
`a18772b feat(epic04+02): Wi-Fi auto-alignment with chirp/music anchor
+ 3-speaker validation`.

- `backend/measurement/startup_tune.py` — band-limited 420 Hz to 3.4 kHz
  linear chirp, 2.65 s, raised-cosine fades, peak amplitude 0.40,
  `TUNE_FILENAME=syncsonic_startup_chirp_v2.wav`.
- `backend/measurement/calibrate_one.py` — per-speaker calibration in
  filter-delay space, dynamic capture sizing (10 s floor, 4 s tail),
  context-aware search window anchored on current filter delay,
  `MIN_CONFIDENCE_SECONDARY=1.2`.
- `backend/measurement/calibrate_sequence.py` — sequential walk over
  every `/tmp/syncsonic-engine/syncsonic-delay-*.sock`, sequence-level
  Sonos muting strategy, anchor-fail aborts the sequence rather than
  silently retargeting, slim `sequence_started` / `sequence_complete`
  payloads to fit BLE ATT MTU.
- `backend/measurement/calibrate_anchor.py` — Sonos acoustic-lag
  measurement in chirp or music mode; result becomes
  `target_total_ms` for the BT loop.
- `backend/syncsonic_ble/state_change/action_request_handlers.py` —
  BLE opcodes `CALIBRATE_SPEAKER` (104), `CALIBRATE_ALL_SPEAKERS`
  (105), `CALIBRATION_RESULT` (115).
- Frontend `app/SpeakerConfigScreen.tsx` — top-of-screen
  "Align all" buttons (startup tune / music), per-speaker inline
  status strip, ref-based completion accumulator that fires the
  `Alert.alert` from per-speaker `applied/failed` events when the
  wrapper `sequence_complete` notification gets MTU-truncated.
- Frontend `hooks/useBLE.ts` — calibration event ring buffer +
  truncation-tolerant JSON parsing.

## Lessons Carried Forward

1. **Filter capacity matters.** Pre-2026-05-01 the C filter clamped
   at 2 s and the actuation manager at 4 s; both have to absorb any
   anchor an output could ever produce, so they were lifted to 5 s.
   When adding a new output type, add its worst-case acoustic lag to
   `MAX_DELAY_MS` first.
2. **Filter-delay space vs user-delay space.** Adjustment math has
   to live entirely in one space. The clean rule is: measure in
   acoustic-lag space, compute adjustment in filter-delay space,
   publish in user-delay space, and let the actuation layer apply
   `TRANSPORT_BASE_MS` once.
3. **MTU is a hard ceiling.** Any BLE notification embedding a
   measurement block must be slimmed to fit ~669 bytes. The
   frontend should still tolerate occasional truncation gracefully
   so a single oversized notification can't kill the whole UX.
4. **Anchor failure with Wi-Fi present must abort.** Falling back
   to a BT-only target when a Wi-Fi anchor was requested actively
   destroys alignment instead of preserving it. Failure modes have
   to surface to the user, not be papered over.
5. **Trust the loud chirp + wide window.** The analyzer does not
   need a guessed expected-peak position to find the dominant
   correlation peak. A wide context-aware window plus a 4 s
   post-signal tail is enough.
