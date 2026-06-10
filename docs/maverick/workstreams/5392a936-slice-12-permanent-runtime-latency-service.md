# Slice 12: permanent runtime latency service

- Workstream branch: `maverick/syncsonic/ultrasonic/slice-12-convert-runtime-latency-from-transient-systemd-run-to-permanent-auto-starting-unit-5392a936`
- Epic lane: `ultrasonic-runtime-sync`
- Updated: `2026-05-31T05:03:18Z`

## Scope

Convert runtime latency correction from an operator-started transient
`systemd-run` command into a permanent auto-starting systemd unit. The runtime
service should wait briefly for speaker availability, then either enter its
burst-emission loop or exit successfully without creating a restart storm.

## Changes

- Added `deploy/runtime-latency.service` with direct `ExecStart` arguments for
  pattern-mode runtime correction, `After=syncsonic.service`,
  `Requires=syncsonic.service`, `Restart=on-failure`, and
  `WantedBy=multi-user.target`.
- Added a startup gate in `backend/measurement/runtime_latency_service.py` that
  polls active connected speaker discovery every 5 seconds for 60 attempts
  before starting mic capture and the measurement loop.
- The service logs `startup_gate_ready`, `startup_gate_waiting`, or
  `startup_gate_timeout`; a timeout exits cleanly with process status `0`.
- Added `test_runtime_service_exits_cleanly_when_no_speakers_connected_after_timeout`.
- Documented install and operational instructions in `backend/README.md`.
- Confirmed `SpeakerActuator.apply()` remains a direct delay update with no
  per-cycle delta clamp beyond the existing `0.0` floor and freak-outlier guard.

## Verification

- `python3 -m compileall syncsonic_ble measurement` passed from `backend/`.
- `git diff --check` passed.
- `python3 -m pytest measurement/test_runtime_latency_service.py measurement/tests/test_slice5_actuator.py`
  was attempted but blocked because this environment's Python does not have
  `pytest` installed.
- `systemd-analyze verify deploy/runtime-latency.service` was attempted but
  could not complete on this host because `syncsonic.service` is not installed
  locally (`Unit syncsonic.service not found`).
- Read-only Pi inspection was attempted:
  - `ssh -o ConnectTimeout=5 syncsonic@10.0.0.89 "git -C /home/syncsonic/SyncSonicPi status --short"`
  - `ssh -o ConnectTimeout=5 syncsonic@10.0.0.89 "systemctl status syncsonic.service --no-pager"`
  - `ssh -o ConnectTimeout=5 syncsonic@10.0.0.89 "journalctl -u syncsonic.service -n 80 --no-pager"`
  All failed with `No route to host`; no remote deployment or runtime
  validation was performed.

## Follow-up

- Run the focused pytest command in an environment with `pytest` installed.
- On the Pi, install the unit, enable it, and verify systemd ordering plus clean
  timeout or active burst-loop behavior from journald.
