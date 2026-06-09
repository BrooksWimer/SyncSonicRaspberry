# Workstream: Slice 9 adaptive burst amplitude ladder for unmeasurable speakers

- Workstream branch: `maverick/syncsonic/ultrasonic/slice-9-adaptive-burst-amplitude-ladder-for-unmeasurable-speakers-858d67ab`
- Epic lane: `ultrasonic-runtime-sync`
- Started: 2026-05-31T04:38:00Z

## Scope

Implement the slice-9 per-speaker ultrasonic burst amplitude ladder as the first audibility-ceiling experiment step:

- Ladder: `amp_x1000 = [300, 600, 950]`
- Escalation threshold: 3 consecutive missed bursts per speaker
- Sticky v1 behavior: successful measurements clear the miss streak but do not reduce an already-escalated speaker amplitude
- No per-cycle delta clamp in `apply()`; the existing correction formula remains a direct delay update with only the physical `0.0` delay floor

## Changes

- Added per-speaker ladder state to `backend/measurement/slice5_actuator.py`.
- Added `burst_amp_x1000_for()` so the runtime service can emit each speaker at its current ladder level.
- Missed-burst application now increments a per-speaker streak and escalates after three misses.
- Pattern-mode burst emission in `backend/measurement/runtime_latency_service.py` now uses the actuator-selected amplitude instead of the global CLI amplitude.
- Added amplitude ladder metadata to runtime journal events and Slice 4 observation snapshots.
- Added actuator tests for three-miss escalation, sticky behavior after recovery, and per-speaker independence.
- Updated the freak-outlier test fixture to match slice 8's 500 ms threshold.

## Verification

- `python3 -m compileall measurement syncsonic_ble` from `backend/`: passed.
- `python3 -m compileall tests/measurement` from repo root: passed.
- `python3 -m pytest measurement/tests/test_slice5_actuator.py measurement/test_runtime_latency_service.py measurement/test_slice4_observer.py` from `backend/`: blocked because this workspace does not have `pytest` installed (`No module named pytest`).
- Direct Python harness invoking every `measurement.tests.test_slice5_actuator.test_*` function: passed.
- Read-only Pi checks attempted:
  - `ssh -o BatchMode=yes -o ConnectTimeout=5 syncsonic@10.0.0.89 "git -C /home/syncsonic/SyncSonicPi status --short"`
  - `ssh -o BatchMode=yes -o ConnectTimeout=5 syncsonic@10.0.0.89 "systemctl status syncsonic.service --no-pager"`
  - `ssh -o BatchMode=yes -o ConnectTimeout=5 syncsonic@10.0.0.89 "journalctl -u syncsonic.service -n 80 --no-pager"`
- Pi validation result: blocked; all SSH checks timed out from this environment.

## Follow-up

Deploy to the Pi and run the audibility-ceiling experiment under continuous music. Required evidence: journal lines showing 300 -> 600 -> 950 escalation on an unmeasurable speaker, no downshift after recovery, and operator/listening confirmation that the 950 ceiling remains acceptable or must be lowered.
