# Workstream: Slice 6 latency baseline slider stage

- Workstream branch: `maverick/syncsonic/ultrasonic/slice-6-latency-baseline-slider-stage-f48cb5ac`
- Lane: `ultrasonic-runtime-sync`
- Status: implementation complete; local verification partial; Pi validation not deployed.

## 2026-05-30

Implemented a slice-6 slider stage in `backend/measurement/slice5_actuator.py` on top of the existing slice-5 ppm path:

- Per-speaker WARMING_UP latency baselines are established from `BASELINE_WARMUP_N` measured-latency samples using the median.
- Runtime latency offset is computed as `measured_latency_ms - baseline_latency_ms`.
- Slider fires `set_delay current_filter_delay_ms - latency_offset_ms` when over `SLIDER_THRESHOLD_MS`, confidence has not failed, no correction stop is asserted, and cooldown is clear.
- Slider cooldown is tracked independently of ppm; ppm actuation remains the existing ACTIVE-state `set_rate_ppm` path.
- Baseline/cooldown reset on operator re-enable and speaker disappearance from the active socket set.
- The emergency stop path now blocks future slider fires, zeros ppm, and restores the pre-slider delay when this actuator has applied a slider correction.
- `runtime_latency_service.py` now passes `measured_latency_ms` into actuator proposals and exposes baseline/cooldown fields in the slice-4 pattern state snapshot.

Added `tests/measurement/test_slice6_actuator.py` with the six requested slice-6 test functions.

Verification:

- `python3 -m compileall backend/measurement backend/syncsonic_ble` passed.
- `python3 -m compileall tests/measurement/test_slice6_actuator.py` passed.
- `python3 -m pytest tests/measurement/test_slice6_actuator.py backend/measurement/tests/test_slice5_actuator.py` blocked because this environment does not have `pytest` installed.
- Direct Python harness execution of all six slice-6 test functions passed.
- Direct Python harness execution of existing slice-5 actuator tests passed.

Pi inspection:

- Pi `10.0.0.89` was reachable.
- `syncsonic.service` was active.
- Remote worktree had dirty/untracked measurement files from prior slice-5 deployment state, including `backend/measurement/runtime_latency_service.py` and an untracked `backend/measurement/slice5_actuator.py`.
- No slice-6 deployment or live slider validation was performed, to avoid overwriting remote dirty state without approval.

Follow-up:

- Decide whether to deploy slice-6 over the current Pi dirty state or first preserve/stash the Pi-local slice-5 artifacts.
- After deployment, run the operator checklist: baseline log confirmation, deliberate 300 ms push on `28:FA`, 30+ minute listening test, SIGUSR1 stop test, and ppm-only regression below threshold.
