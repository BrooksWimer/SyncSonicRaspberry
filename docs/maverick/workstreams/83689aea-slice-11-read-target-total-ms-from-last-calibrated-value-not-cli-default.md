# Slice 11: read target_total_ms from last calibrated value

- Workstream branch: `maverick/syncsonic/ultrasonic/slice-11-read-target-total-ms-from-last-calibrated-value-not-cli-default-83689aea`
- Epic lane: `ultrasonic-runtime-sync`
- Updated: `2026-05-31T04:53:15Z`

## Scope

Runtime ultrasonic correction should use the `target_total_ms` last applied by
startup-tune calibration instead of always using the runtime CLI default.
Per-speaker startup-tune targets take precedence over shared Align All targets.
The CLI default remains the fallback when no persistent target file exists.

## Changes

- Added `backend/measurement/calibration_targets.py` with atomic JSON persistence
  under `/var/lib/syncsonic/startup_tune_targets.json` by default and
  `SYNCSONIC_CALIBRATION_TARGETS_PATH` override support.
- Updated the BLE startup-tune handlers to persist:
  - per-speaker targets after a single-speaker calibration reaches `applied`
  - shared targets when an all-speaker startup-tune sequence starts with its
    effective target, including Wi-Fi anchor-derived targets
- Updated `runtime_latency_service.py` so Slice 5 apply calls resolve
  per-speaker target, then shared target, then CLI default.
- Kept `SpeakerActuator.apply()` as a direct delay update with only the existing
  physical `0.0` floor and freak-outlier guard; no per-cycle delta clamp added.
- Added four unit tests covering missing-file fallback, shared target reads,
  per-speaker precedence, and persistence of shared plus per-speaker values.

## Verification

- `python3 -m compileall syncsonic_ble measurement` passed.
- Dependency-light smoke check for `record_startup_tune_target()` and
  `read_startup_tune_target()` passed.
- `git diff --check` passed.
- `python3 -m pytest measurement/test_runtime_latency_service.py measurement/tests/test_slice5_actuator.py`
  was attempted but blocked because this environment's Python does not have
  `pytest` installed.
- Read-only Pi inspection was attempted:
  - `ssh syncsonic@10.0.0.89 "git -C /home/syncsonic/SyncSonicPi status --short"`
  - `ssh syncsonic@10.0.0.89 "systemctl status syncsonic.service --no-pager"`
  Both failed with `No route to host`; no remote deployment or runtime validation
  was performed.

## Follow-up

- Run the focused pytest command in an environment with pytest installed.
- Deploy to the Pi and verify a startup-tune calibration writes the persistent
  target file, then confirm runtime ultrasonic correction logs
  `runtime_target_total_resolved` with `per_speaker` or `shared` source.
