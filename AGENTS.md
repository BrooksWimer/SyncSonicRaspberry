# SyncSonic Orchestration Doctrine

This file defines the working agreements for AI-assisted workstreams in `SyncSonicPi`.
It is loaded automatically by Codex at session start and should be treated as persistent
project context.

## Project Overview

SyncSonicPi is a dual-surface product repo:

- `backend/` is the Raspberry Pi control plane: a Python BLE/audio orchestration service
  that manages adapters, system audio runtime, device coordination, and service startup.
- `frontend/` is the Expo / React Native control app used to manage devices, settings,
  and user-facing flows.
- Real Raspberry Pi deployment and hardware validation are in scope. Do not treat this
  repo as desktop-only when changes affect BLE, audio routing, adapters, service startup,
  or end-to-end latency behavior.

## Start Here

Read these first when orienting to the repo:

- `README.md`
- `backend/README.md`
- `frontend/README.md`
- `docs/maverick/WORKSTREAM_MODEL.md`
- the matching doc under `docs/maverick/epics/` for the assigned epic lane
- `.agents/skills/verify/SKILL.md`
- `.agents/skills/pi-hardware-verify/SKILL.md` when backend or hardware behavior is involved

## Branch Model

SyncSonic now uses a neutral-foundation plus epic-lane model for Maverick work:

- `foundation/neutral-minimal` is the stable shared base.
- `epic/01-pipewire-transport-stability` is the long-lived merge target for
  PipeWire transport and latency-application stability work.
- `epic/02-startup-mic-auto-alignment` is the long-lived merge target for
  startup audible microphone calibration.
- `epic/03-runtime-ultrasonic-auto-alignment` is the long-lived merge target
  for runtime ultrasonic measurement and correction.
- `epic/04-wifi-speakers-manual-alignment` is the long-lived merge target for
  Wi-Fi speaker discovery, playback, and manual alignment work.

Historical experimental branches are research sources only. Do not merge them
wholesale into an epic branch. Manually transplant scoped code only.

## Core Principles

1. **Verify before claiming done.** Every completed unit of work must include evidence.
   Run the relevant local checks and, when the change affects Pi runtime behavior, include
   remote validation evidence or explicitly explain why it could not be performed.

2. **Maintain the workstream summary.** After every meaningful change, update the
   workstream summary with what changed, what was verified, and what still needs follow-up.

3. **Escalate with options, not questions.** When a human decision is needed, present
   2-3 concrete options with a recommendation instead of asking an open-ended question.

4. **One concern per commit.** Keep commits focused. Split unrelated backend, frontend,
   deployment, and documentation work into separate commits when practical.

5. **Do not guess on hardware claims.** If the change affects BLE advertising, Bluetooth
   pairing, speaker connection, audio fan-out, PipeWire/PulseAudio runtime, or service
   startup, do not claim success from static inspection alone.

6. **Commercial and legal work needs careful framing.** Drafts for commercialization,
   cost mapping, GTM strategy, or patent-related material are in scope, but do not present
   legal conclusions as settled legal advice.

## Verification Baseline

Use the repo-appropriate checks, not generic placeholders.

### Frontend

Run from `frontend/`:

- `npm run lint`
- `npx tsc --noEmit`
- `npx jest --watchAll=false` when tests exist or behavior under test changed

### Backend

Run from `backend/`:

- `python -m compileall syncsonic_ble`
- `python -m pytest` when tests exist for the touched area

### Hardware / Pi Validation

Also perform Pi validation when changes affect:

- BLE discovery, pairing, or GATT behavior
- Bluetooth speaker connection behavior
- latency measurement, ultrasonic sync, or DSP logic
- service startup, adapter reset flow, or runtime environment
- anything intended for deployment on the Raspberry Pi

If Pi validation is relevant, use `.agents/skills/pi-hardware-verify/SKILL.md`.

## Remote Pi Target

The primary deployment target currently known to the repo is:

- Host: `10.0.0.89`
- User: `syncsonic`
- Remote root: `/home/syncsonic/SyncSonicPi`
- Backend working dir: `/home/syncsonic/SyncSonicPi/backend`
- Service: `syncsonic.service`

Preferred remote inspection pattern:

- Use single read-only SSH commands when possible.
- Prefer absolute paths or `git -C ...` over `cd ... && ...`.
- Good examples:
  - `ssh syncsonic@10.0.0.89 "git -C /home/syncsonic/SyncSonicPi status --short"`
  - `ssh syncsonic@10.0.0.89 "systemctl status syncsonic.service --no-pager"`
  - `ssh syncsonic@10.0.0.89 "journalctl -u syncsonic.service -n 200 --no-pager"`

Remote mutating actions such as copying files, restarting services, changing packages,
or editing remote config are in scope, but they should leave a clear approval trail and
must be called out in verification notes.

## Current Strategic Lanes

Since the 2026-05-09 branch-model elevation, SyncSonic has **ten first-class lanes** (six post-North-Star + four historical epics promoted to ongoing-work status). See `docs/maverick/WORKSTREAM_MODEL.md` for the full table and `config/control-plane.shared.json` for the canonical Maverick registration.

**Post-North-Star lanes:**

- `feature-hardening` — operational hardening of the coordinated engine
- `ui-polish` — mobile/Expo fit-and-finish
- `custom-hardware-design` — enclosure / board / BOM (mostly design)
- `patent-application` — patent drafting + prior-art research
- `ultrasonic-runtime-sync` — port ultrasonic playback-time correction into the coordinated engine
- `spatial-audio-awareness` — exploratory: room mapping, mic-driven shape adjustment

**Historical epic lanes elevated 2026-05-09 (v1 in main, forward work ongoing):**

- `pipewire-stability` — ongoing PipeWire transport stability work
- `startup-mic` — chirp + music anchor calibration improvements
- `runtime-ultrasonic` — historical experimentation lane (parallel to ultrasonic-runtime-sync)
- `wifi-manual` — Wi-Fi speakers beyond Sonos

All ten lanes share the same Pi-validation rule, verification baseline, and workstream protocol documented in the rest of this file. Feature implementation should stay inside one lane at a time.

Other product work such as commercial readiness, cost mapping, and GTM planning may still happen, but should not mix into a feature-implementation workstream.

## Workstream Protocol

When working within an orchestrated workstream:

- **Intake**: Convert the request into scope, acceptance criteria, and risks.
- **Planning**: Break the work into implementable steps and identify whether Pi validation
  is required for true completion.
- **Epic discipline**: Confirm which epic lane owns the request. If the work spans
  multiple lanes, recommend a split instead of blending concerns into one branch.
- **Implementation**: Make focused changes and keep backend, frontend, and deployment
  concerns separated when possible.
- **Verification**: Run the relevant local checks first, then Pi validation if applicable.
- **Review**: Summarize the code change, deployment implications, and evidence.

## Safety Defaults

- Do not run destructive commands locally or remotely without explicit approval.
- Do not install new dependencies locally or on the Pi without approval.
- Do not present remote deployment as successful unless you have remote evidence.
- Treat remote host access as production-adjacent: inspect first, mutate second.
- Keep web research as supporting evidence, not a substitute for repo or hardware truth.

## Logging

- Log every significant action and decision with rationale.
- When using subagents, include their summaries in the parent workstream log.
- Include timestamps, verification commands, and relevant file paths in status updates.
