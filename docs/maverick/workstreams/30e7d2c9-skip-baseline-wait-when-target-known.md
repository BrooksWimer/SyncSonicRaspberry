# Workstream: Skip baseline wait when target known

- Workstream branch: `maverick/syncsonic/ultrasonic/skip-baseline-wait-when-target-known-30e7d2c9`
- Epic: `ultrasonic-runtime-sync`
- Updated: 2026-06-10

## Summary

- Reduced `DYNAMIC_TARGET_BASELINE_N` from 5 to 3 in `backend/measurement/runtime_latency_service.py`.
- Added `RuntimeSyncService._target_from_persistence` to remember when the runtime target came from a persisted shared or per-speaker startup-tune target.
- Runtime correction now accumulates the first baseline sample but skips the cold-start `return None` when a persisted target exists, allowing the actuator to run immediately after service restart.
- Cold starts without a persisted target still wait for baseline samples, now firing on the 3rd sample instead of waiting for a 4th call.

## Verification

- `python3 -m pytest backend/measurement/ -q` — passed, `61 passed in 1.47s`.

## Follow-up

- Pi/runtime validation was not run in this turn because the requested verification scope was the backend measurement pytest target.
