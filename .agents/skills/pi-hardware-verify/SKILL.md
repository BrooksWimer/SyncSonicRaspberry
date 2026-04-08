---
name: pi-hardware-verify
description: Validate SyncSonic backend and deployment changes against the real Raspberry Pi target using local checks plus remote evidence.
---

# Pi Hardware Verify

Validate SyncSonic changes against the real Raspberry Pi target when backend,
deployment, BLE, audio, or end-to-end latency behavior is involved.

## When to use

Use this skill when:

- backend code changes affect BLE, Bluetooth, GATT, device discovery, or connection handling
- audio runtime changes touch PipeWire, PulseAudio, DSP, latency, or ultrasonic measurement
- deployment changes affect `start_syncsonic.sh`, `syncsonic.service`, adapter reset flow, or runtime environment
- a task claims hardware readiness, deployment readiness, or end-to-end validation

## Remote target

- Host: `10.0.0.89`
- User: `syncsonic`
- Remote root: `/home/syncsonic/SyncSonicPi`
- Service: `syncsonic.service`

## Process

1. **Run local checks first** so obvious syntax or lint issues do not get confused with Pi/runtime problems.
2. **Inspect before mutating** using read-only SSH commands.
3. **If deployment is required**, summarize the exact remote actions and request approval before mutating the Pi.
4. **Collect evidence** from service state, logs, and any relevant Bluetooth/runtime status.
5. **Report clearly** whether verification passed, failed, or was blocked by connectivity or hardware limits.

## Preferred inspection commands

Prefer single read-only commands. Avoid `cd ... && ...` when you can use absolute paths.

Examples:

- `ssh syncsonic@10.0.0.89 "git -C /home/syncsonic/SyncSonicPi status --short"`
- `ssh syncsonic@10.0.0.89 "systemctl status syncsonic.service --no-pager"`
- `ssh syncsonic@10.0.0.89 "journalctl -u syncsonic.service -n 200 --no-pager"`
- `ssh syncsonic@10.0.0.89 "bluetoothctl show"`
- `ssh syncsonic@10.0.0.89 "bluetoothctl devices"`

## Mutating remote actions

Remote mutations are in scope, but they require extra care. Examples include:

- uploading or syncing files
- restarting or reloading services
- package installation
- modifying system configuration
- changing Bluetooth adapter state in a way that alters the runtime

When mutating:

- state exactly what will change
- keep commands tightly scoped
- capture before and after evidence
- follow with read-only verification commands

## Output expectations

Produce a short verification note that includes:

- which local checks were run
- which remote commands were run
- whether the Pi was reachable
- service and log evidence gathered
- whether the task is truly verified, partially verified, or blocked

## Important

- Do not claim hardware success without remote evidence.
- If the Pi is unreachable, say so explicitly and treat hardware validation as blocked.
- Prefer inspection commands that can be auto-approved; reserve complex remote actions for explicit approval.
