---
name: verify
description: Run SyncSonic verification checks, capture evidence, and determine whether a workstream is ready for review.
---

# Verify

Run verification checks and produce structured evidence of pass/fail status.

## When to use
Use this skill after implementation is complete, before moving to Review state.
Also use after applying review feedback to confirm fixes.

## Process

1. **Identify verification targets**: What should be checked? (tests, lint, build, types, etc.)
2. **Run checks**: Execute each verification command and capture output.
3. **Analyze results**: Parse output for failures, warnings, and errors.
4. **Produce evidence report**: Structured summary of what passed and what failed.
5. **Decide**: If all pass → ready for review. If any fail → return to implementation with specific fix targets.

## Project-specific verification map

Use these commands for SyncSonic instead of generic placeholders.

### Frontend

Run from `frontend/`:

- `npm run lint`
- `npx tsc --noEmit`
- `npx jest --watchAll=false` when tests exist or touched behavior is covered by tests

Notes:

- Do not use `npm test` as-is for verification because this repo's script starts Jest in watch mode.
- If frontend work is visual only and there are no meaningful tests for the touched path, note that explicitly.

### Backend

Run from `backend/`:

- `python -m compileall syncsonic_ble`
- `python -m pytest` when tests exist for the touched area

Notes:

- Desktop-only syntax checks are not enough for changes that affect BLE, audio runtime,
  service startup, or Raspberry Pi deployment behavior.
- For backend runtime changes, pair local verification with `.agents/skills/pi-hardware-verify/SKILL.md`.

### Pi / hardware validation

Use Pi validation when changes affect:

- BLE or Bluetooth device discovery
- GATT characteristics or phone ↔ Pi control flow
- speaker connection or disconnection behavior
- latency control, ultrasonic sync, PipeWire, PulseAudio, or adapter reset logic
- `start_syncsonic.sh`, `syncsonic.service`, or deployment/runtime assumptions

Remote read-only checks commonly used here:

- `ssh syncsonic@10.0.0.89 "systemctl status syncsonic.service --no-pager"`
- `ssh syncsonic@10.0.0.89 "journalctl -u syncsonic.service -n 200 --no-pager"`
- `ssh syncsonic@10.0.0.89 "git -C /home/syncsonic/SyncSonicPi status --short"`

### Epic-aware verification reminders

- `foundation/neutral-minimal` is intentionally Bluetooth-only with manual
  delay. Do not treat Wi-Fi or microphone behavior as expected on that branch.
- Epic 1 should reintroduce stripped PipeWire infrastructure only when there is
  a concrete validation purpose and evidence path.
- Epic 2 and Epic 3 require Raspberry Pi validation before claiming success.
- Epic 4 should prove Wi-Fi discovery/playback/manual alignment behavior before
  coupling it to microphone automation.

## Verification commands (detect from project)

- **Tests**: Look for `npm test`, `pytest`, `go test`, `cargo test`, or project-specific test commands
- **Lint**: Look for `eslint`, `ruff`, `golangci-lint`, or similar
- **Build**: Look for `npm run build`, `tsc`, `go build`, `cargo build`
- **Types**: Look for `tsc --noEmit`, `mypy`, `pyright`

## Output format

```markdown
## Verification Report
**Status**: PASS / FAIL
**Checks run**:
- [x] Tests: [pass/fail] - [summary]
- [x] Lint: [pass/fail] - [summary]
- [x] Build: [pass/fail] - [summary]
- [x] Types: [pass/fail] - [summary]

**Failures** (if any):
- [description of failure + suggested fix]

**Recommendation**: [ready-for-review / needs-fixes]
```

## Important
Never mark verification as passed if any check fails.
Never skip checks that are available in the project.
If a check is not applicable, note it as "N/A" with reason.
Never mark Pi-sensitive backend changes as fully verified without either remote evidence or an explicit blocker.
