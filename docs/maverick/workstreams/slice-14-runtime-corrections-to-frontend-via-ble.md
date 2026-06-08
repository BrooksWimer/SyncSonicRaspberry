# Slice 14: surface runtime corrections to frontend via BLE

- Workstream branch: `slice-14-surface-runtime-corrections-to-the-react-native-frontend-via-ble-6e5641ba`
- Epic lane: `ultrasonic-runtime-sync`
- Updated: `2026-05-31T21:57:28Z`

## Scope

Surface runtime ultrasonic delay corrections from the permanent runtime latency
service path to the React Native speaker configuration screen without
throttling. The backend writes corrected actuation events to JSONL, the BLE
runtime tails that stream and emits `CALIBRATION_RESULT`, and the frontend
updates each affected speaker card as corrections arrive.

## Changes

- Added corrected-only JSONL appends in `backend/measurement/slice5_actuator.py`
  at `/run/syncsonic/runtime_corrections.jsonl`.
- Added `backend/syncsonic_ble/runtime_corrections.py`, a daemon watcher that
  tails the JSONL file and emits `CALIBRATION_RESULT` (`0x73`) with
  `phase=runtime_correction`.
- Wired the runtime-correction watcher into `backend/syncsonic_ble/main.py`
  beside the existing telemetry and coordinator background workers.
- Updated `frontend/app/SpeakerConfigScreen.tsx` to handle
  `runtime_correction` events by animating the latency slider to
  `new_filter_delay_ms`, incrementing a per-speaker correction count, and
  flashing a correction badge.
- Added focused backend tests for JSONL event writing and watcher payload
  forwarding.

## Verification

- `python3 -m compileall syncsonic_ble measurement` passed from `backend/`.
- `git diff --check` passed.
- `python3 -m pytest measurement/tests/test_slice5_actuator.py syncsonic_ble/tests/test_runtime_corrections.py`
  was attempted but blocked because this environment does not have `pytest`
  installed (`No module named pytest`).
- `npm run lint` was attempted from `frontend/` but blocked because
  `node_modules/.bin/expo` is missing.
- `npx tsc --noEmit` was attempted from `frontend/` but blocked by missing
  frontend dependencies, including `expo/tsconfig.base`, React Native modules,
  and Jest globals.
- `npx jest --watchAll=false` was attempted from `frontend/` but blocked because
  `jest-expo` is not installed in `node_modules`.
- Read-only Pi inspection succeeded:
  - `ssh -o ConnectTimeout=5 syncsonic@10.0.0.89 "git -C /home/syncsonic/SyncSonicPi status --short"`
    returned `?? backend/tools/pw_delay_filter`.
  - `ssh -o ConnectTimeout=5 syncsonic@10.0.0.89 "systemctl status syncsonic.service --no-pager"`
    showed `syncsonic.service` active/running since `2026-05-31 16:15:36 EDT`.
  - `ssh -o ConnectTimeout=5 syncsonic@10.0.0.89 "journalctl -u syncsonic.service -n 120 --no-pager"`
    showed current BLE connection/control-plane activity and PipeWire runtime
    logs.

## Follow-up

- Install frontend dependencies locally before rerunning lint, TypeScript, and
  Jest.
- Install pytest in the backend environment or use the project test image before
  rerunning the focused backend tests.
- Deploy this branch to the Pi and verify live `runtime_correction`
  `CALIBRATION_RESULT` notifications during runtime latency corrections before
  claiming hardware completion.
