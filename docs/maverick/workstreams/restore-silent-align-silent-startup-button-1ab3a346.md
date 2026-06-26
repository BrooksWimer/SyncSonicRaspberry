# Workstream: Restore Silent Align silent-startup button

_Status recorded by Claude on 2026-06-25._

- Workstream: restore-silent-align-silent-startup-button-1ab3a346
- Epic: ultrasonic-runtime-sync
- Branch: maverick/syncsonic/ultrasonic/restore-silent-align-silent-startup-button-1ab3a346
- Base: ultrasonic-runtime-sync

## Summary

Restored the user-triggered **Silent Align** button and its bounded one-shot
fast-align behaviour, transplanted from research branch
`origin/correction-hardening` (commit `c5898be`) onto the
`ultrasonic-runtime-sync` lane. The button was dropped from `main` when
continuous ultrasonic auto-align became the default; this brings back the
explicit "align once, then settle" pass (fewer false adjustments during
steady-state playback).

Only the silent-align slice of `c5898be` was transplanted. The
holdoff / adaptive-clamp / dynamic-target actuator tuning that shipped in the
same commit was **not** re-applied — it already exists on the lane, and
`slice5_actuator.py` is unchanged here.

## Changes

- Frontend
  - `ble_constants.ts`: `START_SILENT_ALIGN: 0x6B`.
  - `ble_functions.ts`: `startSilentAlign(device)`.
  - `SpeakerConfigScreen.tsx`: `silentAlignPhase` state, `silent_align_started` /
    `silent_align_complete` `CALIBRATION_RESULT` handlers, `handleSilentAlign`,
    and the **Silent Align** button cycling idle → `Aligning...` → `Aligned ✓`.
- Backend BLE
  - `constants.py`: `START_SILENT_ALIGN = 0x6B`.
  - `action_request_handlers.py`: `handle_start_silent_align` creates
    `/run/syncsonic/silent_align_requested` and emits an immediate
    `silent_align_started` notification; registered in `HANDLERS`.
  - `runtime_corrections.py`: already forwards both silent-align phases
    (landed via PR #39) — left unchanged.
- Runtime service (`runtime_latency_service.py`)
  - Replaced the vestigial read-only `_refresh_fast_align_state` (which only
    probed `.exists()` and never exited) with a **bounded one-shot**: on trigger
    it `unlink()`s the file, resets per-speaker measurement windows / holdoff /
    dynamic-target baselines, runs at `FAST_ALIGN_CADENCE_SEC` with
    `FAST_ALIGN_CONFIDENCE_WINDOW_N`, and exits after two consecutive converged
    cycles (`_maybe_complete_fast_align`), writing `silent_align_complete` to
    `runtime_corrections.jsonl`.
  - `_measure_once` / `_measure_pattern` now return the `ActuationResult` so the
    measurement loop can collect per-cycle actions for the convergence check.
  - Added `test_fast_align_bounded_exit_writes_silent_align_complete`.

## Verification

- Backend
  - `python -m compileall syncsonic_ble measurement` — clean.
  - Bounded fast-align logic verified directly (SIGUSR1 stubbed for the
    Windows import): two consecutive converged cycles exit and write
    `silent_align_complete`; a `corrected` cycle resets the converged counter;
    the entry path consumes (unlinks) the trigger and activates fast-align.
  - `python -m pytest measurement` — **blocked locally**: collection fails on the
    pre-existing `signal.SIGUSR1` reference in `slice5_actuator.py:91`
    (not introduced here; Windows has no `SIGUSR1`). The new unit test runs on
    Linux / Pi / CI.
- Frontend
  - `npx tsc --noEmit` — 0 errors.
  - `npx jest --watchAll=false` — 15/15 (incl. `protocol-alignment.test.ts`
    confirming `START_SILENT_ALIGN = 0x6B` matches on both sides).
  - `npm run lint` — 0 errors (86 pre-existing warnings, none new).

## Pi validation

Not run. Operator-gated per the `fast-align-speed-improvement` precedent.
After lane promote, validate on `syncsonic@10.0.0.89`:

1. Press **Silent Align** in the app.
2. Confirm `/run/syncsonic/silent_align_requested` is created (BLE handler) and
   consumed by the runtime-latency service within one cycle.
3. Confirm the service enters fast-align (1 s cadence), converges, and emits
   `silent_align_complete`; the button shows `Aligned ✓`.
4. Confirm continuous auto-align resumes afterward (the bounded pass does not
   disable steady-state correction).

## Follow-up

- Run `pytest measurement` on Linux/Pi to exercise the new bounded-exit test.
- Pi end-to-end validation of the BLE round-trip and bounded convergence above.
