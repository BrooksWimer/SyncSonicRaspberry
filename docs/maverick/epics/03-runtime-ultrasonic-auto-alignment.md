# Epic 03: Runtime Ultrasonic Auto-Alignment

> **Status: NEXT ACTIVE LANE (queued 2026-05-01).** Promoted now that
> Epic 02 (startup mic auto-alignment) and Epic 04 (Wi-Fi speakers)
> have shipped. The project owner has flagged this as the highest-value
> remaining audio-stack feature for keeping the system stable across
> long playback sessions. See `../ROADMAP.md` §3.3 for strategic
> framing.

## Goal

Support runtime microphone-based correction using ultrasonic bursts while music
is playing, without disrupting the listening experience or bypassing the shared
actuation boundary.

## Why This Matters Now

BT speakers drift on the order of 20-80 ppm against each other,
accumulating tens of ms of relative offset over an hour. The Slice 2
elastic engine plus the Slice 3 coordinator can already correct drift
deterministically — what is missing is the **measurement signal**
during music playback. Today's calibration is an explicit user action
(button press, brief audible chirp); runtime ultrasonic alignment
closes the loop continuously and silently.

Importantly, this is **not blocking the dream.** Today's startup +
on-demand calibration delivers the North Star; ultrasonic runtime
alignment is the upgrade that keeps the dream stable across hours of
playback without user intervention.

## In Scope

- ultrasonic burst generation and capture
- runtime measurement while playback is active
- bounded runtime correction logic
- UX and reporting specific to runtime auto-alignment

## Out of Scope

- startup audible calibration
- Wi-Fi speaker discovery/connection work
- unrelated transport refactors unless they are strictly required for runtime
  correction to function safely

## Starting Point

**Branch off `epic/05-coordinated-engine`** (commit `a18772b` or later),
**not** `foundation/neutral-minimal`. The Slice 2 elastic delay engine,
the Slice 3 coordinator, and the Slice 4 cross-correlation analyzer
are the substrate this epic builds on; forking back to neutral-minimal
would re-derive all of that.

Reuse intact from Epic 02 / 04:
- `backend/measurement/lag_analyzer.py` — FFT-based cross-correlation
  with `confidence_primary` / `confidence_secondary` discrimination
- `backend/measurement/calibrate_one.py::_lag_search_window` — wide
  context-aware search window pattern, no expected-peak centring
- per-speaker `pw_delay_filter` Unix sockets at
  `/tmp/syncsonic-engine/syncsonic-delay-<token>.sock` accept
  `set_delay` and `set_rate_ppm` live without xrun (Slice 2)

Reuse with adaptation:
- the chirp WAV generator (`startup_tune.py`) generalises to a
  high-frequency burst generator if we go ultrasonic, or to a
  short broadband click if we go "chirp inserted into musically-
  quiet regions"
- the calibrate_one capture pattern (`_capture_pair`) generalises to
  a passive measurement pattern that does not mute peers

## Open Design Questions (resolve before slice 0)

1. **Ultrasonic vs in-band chirp**: ultrasonic (>= 18 kHz) is
   inaudible to most adults but some BT speakers brick-wall at
   16 kHz, and pets can hate the bursts. In-band chirp (a 2 ms
   click in a musically-quiet region detected via the existing
   `virtual_out.monitor` envelope) avoids those failure modes but
   is less deterministic in timing. **Recommendation**: prototype
   both, decide with telemetry.
2. **Measurement cadence**: 1 Hz? 0.1 Hz? 0.01 Hz? The drift rate
   under normal conditions is ~20-80 ppm; at 0.01 Hz we'd correct
   tens of ms per minute, which is enough to hold alignment across
   an hour. Higher cadences burn CPU on FFTs.
3. **Correction mechanism**: bounded rate adjustment (existing
   ±50 ppm cap from ROADMAP §4) is the right primary tool. Filter
   delay jumps during music are forbidden by design (audible click).
4. **UI surface**: a single switch in `SpeakerConfigScreen.tsx` plus
   a small status pill ("drift correction: on, ±2 ppm avg over last
   minute") via the existing `coordinatorState` BLE notification.

## Validation Expectations

- local backend/frontend checks as applicable
- Raspberry Pi validation is mandatory
- evidence should show that runtime correction works during playback
  and does not degrade the stable baseline experience
- specifically: 1 hour of continuous music playback shows < 5 ms of
  cumulative inter-speaker drift with runtime correction enabled,
  vs the unmitigated drift that the Slice 1 telemetry can quantify
  with correction disabled (A/B test, same hardware, same music)
