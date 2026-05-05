# SyncSonic Project Memory

Durable cross-workstream facts, decisions, and conventions. Operator-editable; planning agents read this before each slice.

## 2026-05-01 - North Star reached on PipeWire stack

- 3-speaker (2 BT + 1 Wi-Fi Sonos) auto-aligned playback validated end-to-end on Pi `syncsonic@10.0.0.89`.
- Epic 02 (startup mic auto-alignment) marked done.
- Epic 04 (Wi-Fi speakers) folded into Epic 02; Sonos is a peer engine output through the coordinated engine, not a separate manual-slider lane.
- Epic 01 (PipeWire transport stability) implicitly satisfied by the coordinated engine; not a separate lane anymore.
- Epic 03 (runtime ultrasonic) remained as the next active technical lane, re-cast as `ultrasonic-runtime-sync` in the new branch model.

## 2026-05-05 - Branch model rewrite

- Created `main` from `epic/05-coordinated-engine` (commit `3eb21cc seems to be working`). The Pi was confirmed running exactly that branch via SHA-1 file hashes (only diff is line endings on `analyze_lag.py`).
- Set `main` as the production branch and the default base for all new workstreams.
- Old `epic/01..05` and `foundation/neutral-minimal` branches retained in git as historical references — never delete them. They remain authoritative for any future "what did the system look like at this milestone" question.
- Maverick config `control-plane.shared.json` rewritten with six new durable epics (`feature-hardening`, `ui-polish`, `custom-hardware-design`, `patent-application`, `ultrasonic-runtime-sync`, `spatial-audio-awareness`) all branching from `main`.
- The Pi service is unaffected by the branch rewrite — it doesn't pull from git, it gets manual `tar`-snapshot deploys, and the file content on disk is identical to `main`.

## Historical epic charters

The five files under `epics/01..05-*.md` describe the lanes before the 2026-05-05 rewrite. They are preserved for context and should not be deleted. New planning calls reference the new charters under the same `epics/` directory.

## Durable conventions

### Pi validation is mandatory

Per `AGENTS.md`: any change touching BLE, audio routing, latency, or service startup must produce Pi validation evidence (journal excerpts + timestamps) on `syncsonic@10.0.0.89`. Local checks (`compileall`, lint) are necessary but never sufficient. Slice 0's Pi validation surfaced two bugs that no static check could have caught (multi-path BlueZ entries, late-binding Python closure under threading).

### Pre-deploy snapshot pattern

Every Pi deployment first does:
```
ssh syncsonic@10.0.0.89 'cd /home/syncsonic && tar -czf snapshots/SyncSonicPi-PRE-<commit>-<timestamp>.tar.gz SyncSonicPi/backend'
```
Snapshots accumulating in `~/syncsonic-snapshots/` is a known disk-pressure issue at month 6 — formalizing rotation is part of `feature-hardening` H7.

### Telemetry destination

Telemetry events stream to `/home/syncsonic/syncsonic-telemetry/events/*.jsonl`. The events table grows forever today; rotation + a "last N hours as a tarball" CLI is part of `feature-hardening` H3.

### Public naming

The product is **SyncSonic**. There is no separate internal codename. The operator has not yet committed to a public-vs-internal naming split.

### Hardware target

Pi 4 + 4× USB Bluetooth controllers + 1× USB measurement microphone, optionally 1× Wi-Fi Sonos. Going to Pi 5, more controllers, or a custom carrier is a deliberate design decision documented in `ROADMAP.md` §3.6 and the active `custom-hardware-design` epic, not an undertaken migration.

### Open questions carried forward (from `ROADMAP.md` §6)

These are intentionally deferred and will be answered with telemetry data, not opinion:

- On-board UART BT controller (`hci3`) versus a USB controller for advertising — would freeing the on-board controller reduce dropouts?
- Single USB 2.0 host as the most likely physical bottleneck for 4 BT controllers + mic.
- `bluez_output.F4..C8.1` accumulated `ERR 1` over 4h55m; Slice 1 telemetry should surface what that error was.
- PipeWire 1.2.7 / WirePlumber 0.4.13 are older than upstream; upgrade to 0.5+ requires the new script-based form.
- Going to 4+ BT speakers requires a hardware decision (more dongles, different SoC, custom carrier); deferred until M1+M2 prove the architecture.

### Design principles that bind every slice (from `ROADMAP.md` §4)

1. Treat the system as one coordinated whole, not as N independent speakers.
2. Audio paths must be deterministic at the design level, not "deterministic if WirePlumber feels like it today."
3. Every claim about "this sounds better" has to be measured.
4. The control plane is the source of truth, not the JSON file on disk.
5. Reserve fast-lane response budget for the things humans hear.
6. Phone audio is not a speaker — the reserved adapter is a control plane.
7. Pi validation is mandatory for BLE / audio / latency / startup changes.
8. Keep optionality at every horizon boundary.
