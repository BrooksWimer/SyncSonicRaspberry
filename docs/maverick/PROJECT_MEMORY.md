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

## 2026-05-09 - Lane structure: ten first-class lanes, not six

- The four lanes inherited from the pre-rewrite epic structure (`pipewire-stability`, `startup-mic`, `runtime-ultrasonic`, `wifi-manual`) are now **first-class lanes alongside the six post-rewrite ones**, not historical/retired ones.
- Their v1 implementations were a success and are merged into the coordinated engine on `main`. That's the floor for this work, not the ceiling. Each lane has ongoing forward work (e.g., `wifi-manual` extends to TVs and other Wi-Fi audio targets beyond Sonos; `pipewire-stability` tracks ongoing drift across PipeWire upgrades; `startup-mic` covers ongoing mic calibration improvements; `runtime-ultrasonic` carries the v1 experimentation context for revisits adjacent to `ultrasonic-runtime-sync`).
- Branch convention: each first-class lane has a branch named identically to the lane id, all based off the latest `main`. Old `epic/01..04` branches are kept as historical references; new work branches from `main`, not from those.
- Total durable lanes: 10. Maverick `control-plane.shared.json` registers all 10 with their respective Discord forum threads.

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

## Debugging guide — operational context for planning agents

This section gives a planning agent (or any operator) enough operational grounding to write a real debug plan rather than a generic one. **When a planning agent is asked to debug a SyncSonic issue, its first move is to consult this guide and pick the relevant playbook.**

### How to reach the Pi

```
ssh syncsonic@10.0.0.89
```

- Repo on Pi: `/home/syncsonic/SyncSonicPi/`
- Backend: `/home/syncsonic/SyncSonicPi/backend/`
- Service unit: `syncsonic.service`
- Snapshots: `/home/syncsonic/snapshots/`
- Telemetry: `/home/syncsonic/syncsonic-telemetry/`

### Where logs live

Three log streams. The first two are primary.

1. **systemd journal** — all Python `logging` output from `syncsonic_ble.main` and modules.
   ```
   journalctl -u syncsonic.service -n 200 --no-pager
   journalctl -u syncsonic.service -f                       # follow
   journalctl -u syncsonic.service --since "10 min ago"
   ```
2. **Telemetry JSONL** — structured CloudEvents-shaped event stream, one line per event.
   ```
   ls /home/syncsonic/syncsonic-telemetry/events/           # latest file is current run
   tail -f /home/syncsonic/syncsonic-telemetry/events/syncsonic-events-*.jsonl | jq '.'
   ```
   Filter to specific subsystems: `jq 'select(.event_type | test("^bluez|^pw_|^coordinator"))'`.
3. **Mic capture rolling buffer** — `~/syncsonic-telemetry/mic/rolling-<idx>.wav`, used post-facto by calibration. Not relevant for connect/playback debug.

There is no `/var/log/syncsonic/*` or syslog entry — only journal and JSONL.

### What runs when the system is "working"

Boot sequence (`backend/syncsonic_ble/main.py`):

1. PipeWire / WirePlumber audio server started.
2. D-Bus + BLE adapters initialized.
3. Pairing agent registered at `/org/bluez/SyncSonic/PhonePairingAgent`.
4. GATT service + characteristics registered (mobile app talks to these).
5. Telemetry collector thread (100 ms tick).
6. System Coordinator thread (10 Hz tick, soft-mute policy owner).
7. GLib main loop.

Child processes when active: `pipewire`, `wireplumber`, `pipewire-pulse`, `pw_delay_filter` (one per connected speaker), `python3 -m measurement.mic_capture`.

### Diagnostic playbook — "speaker is connected but not playing audio"

This is the canonical first failure to debug. Run these in order; first failure is the root cause.

| Step | Command (run on Pi) | What it tells you |
|---|---|---|
| 1 | `bluetoothctl info <SPEAKER_MAC>` | `Paired: yes` + `Connected: yes` confirms the BLE ACL link is up. If `Connected: no`, the BlueZ-level connect failed; jump to journal for `action_functions.py` errors. |
| 2 | `pactl list sinks short \| grep <MAC_no_colons>` | Should show `bluez_output.<MAC_underscores>.a2dp_sink`. If missing, A2DP codec negotiation has not completed. **JBL models prefer SBC**; if PipeWire is offering an unsupported codec (e.g. aptX without license), the sink never appears. Check `pactl list sinks verbose` for the actual codec offered. |
| 3 | `pgrep -af "pw_delay_filter.*<MAC>"` | Should show one process per connected speaker. If absent, `pipewire_transport.py` did not start the filter — see `_start_filter()` errors in journal. |
| 4 | `ls /tmp/syncsonic-engine/syncsonic-delay-*.sock` | The filter's Unix control socket. If filter is running but socket is missing, filter crashed during init. |
| 5 | `tail /home/syncsonic/syncsonic-telemetry/events/syncsonic-events-*.jsonl \| jq 'select(.event_type == "coordinator_tick") \| .data.speakers[]'` | Coordinator's view of every connected speaker: `health_state` (HEALTHY/STRESSED/MUTED), `frames_in`, `frames_out`, `rssi_db`. If `MUTED`, audio is intentionally being held back by the soft-mute policy. |
| 6 | `pw-link -l \| grep <MAC>` | Verifies the route from the virtual phone-input sink → delay filter → speaker sink. If links are missing, `_connect_route()` failed. |

### Common failure-mode → fix map

- **"Paired but won't connect"** — BlueZ adapter stuck. The service runs `reset_bt_adapters.sh` as a pre-start hook (`hciconfig <adapter> reset`); a manual `sudo systemctl restart syncsonic` re-runs it.
- **"Connected but no audio"** (the JBL case in scope) — Almost always **A2DP codec negotiation**. Wait 3-5 seconds after connect; some JBL models take that long. If the sink never appears, codec mismatch — check `pactl list sinks verbose`.
- **"RSSI low, audio cutting out"** — Coordinator soft-mutes when RSSI dips ≥5 dB below 60-second median AND frame underrun is detected. Check `coordinator_tick` events for `rssi_db` and `health_state=STRESSED`. Move the Pi closer or away from 2.4 GHz interference.
- **"Multiple instances of same MAC on different controllers"** — `connect_one_plan()` keeps one, disconnects others. If failure persists, check `BLUEZ_DISCONNECT` events in telemetry.

### Useful one-liners

```
# Service health + recent log
ssh syncsonic@10.0.0.89 "systemctl status syncsonic.service --no-pager && journalctl -u syncsonic.service -n 100 --no-pager"

# Live coordinator state (one second per line)
ssh syncsonic@10.0.0.89 "tail -f /home/syncsonic/syncsonic-telemetry/events/syncsonic-events-*.jsonl | jq 'select(.event_type==\"coordinator_tick\") | {n: .data.n_speakers, speakers: [.data.speakers[] | {mac: .mac[-5:], state: .health_state, rssi: .rssi_db, frames_out: .frames_out}]}'"

# All BLE-related errors in last 30 minutes
ssh syncsonic@10.0.0.89 "journalctl -u syncsonic.service --since '30 min ago' | grep -iE 'error|warn|fail' | grep -iE 'bluez|connect|pair'"

# Snapshot the current Pi state before changing anything
ssh syncsonic@10.0.0.89 "cd /home/syncsonic && tar -czf snapshots/SyncSonicPi-PRE-debug-\$(date +%s).tar.gz SyncSonicPi/backend"
```

### What's NOT covered yet

These are real gaps in operational tooling. A planning agent should suggest creating these as workstreams under `feature-hardening` if the debugging would benefit from them:

- No CLI tool prints "current state of all speakers" — you have to grep the JSONL stream.
- No `/devices` BLE characteristic for the app to poll on-demand (today the app only sees push notifications).
- No automatic snapshot of telemetry around a failure event (would help post-facto debug).
- Telemetry events table grows forever — rotation is `feature-hardening` H3, not done.

### Important — what NOT to do during debug

- **Do not delete BlueZ paired devices** unless explicitly told to. The trust + bond data is what enables auto-reconnect.
- **Do not restart `pipewire` / `wireplumber` directly** while a route is live. Restart `syncsonic.service` instead — it tears down filters cleanly.
- **Do not modify code paths in `connection_manager.py` or `pipewire_transport.py`** without taking a pre-deploy snapshot first (per the snapshot pattern above).
- **Do not edit logs/telemetry directly** to "make a problem look fixed." All claims of fix require fresh Pi validation evidence per AGENTS.md.

## 2026-05-14 — Backend mypy baseline closed to zero errors (Pi-validated)

Three passes through 2026-05-14 brought `python -m mypy --config-file backend/pyproject.toml syncsonic_ble measurement` from **20 errors → 12 → 0** across 55 source files. Full audit history lives in `backend/MYPY_AUDIT.md` (kept as the canonical record). High-level closure:

- Pass 1: established `pyproject.toml` `[tool.mypy]` config + per-module overrides for the `socket.AF_UNIX` Windows-typeshed false positive and the `DBusPathMixin` attribute pattern. Documented the 20-error baseline.
- Pass 2: removed 6 stale `# type: ignore` comments (handled by `ignore_missing_imports = true` now) + 2 trivial empty-literal annotations (`config_speaker_usage: dict[str, list[str]]`, `characteristics: list[dbus.service.Object]`).
- Pass 3 (Pi-validated): closed the remaining 12 — `constants.reserved` narrowed at source so every `from … import reserved` resolves to `str`; `_require_bus()` helper raises a clear `RuntimeError` instead of letting `None.get_object(...)` crash deep inside dbus; explicit `device_path: str | None` declaration plus a guard before pair/trust/connect/remove that redirects to discovery if None; `_property_changed` resolves path once into `resolved_path` before passing to `_handle_new_connection`; `find_actual_sink_name() -> str | None` matches the function's real behavior. Pi-validated by fetching the branch into `/tmp/syncsonic-narrowing-check` on `syncsonic@10.0.0.89`, running `compileall` + smoke import that exercises both the constants narrowing and the `_require_bus()` error path; service was not restarted, fix flows to next deploy.

Per `backend/MYPY_AUDIT.md`'s closing note: `check_untyped_defs = true` is now an isolated next-step decision rather than gated on fixing N existing bugs first.

## 2026-05-14 — BLE protocol documented + cross-stack alignment now in CI

`docs/BLE_PROTOCOL.md` was added today as the reverse-engineered wire-protocol reference. Captures:

- Service / Characteristic / CCCD UUIDs.
- Wire format diagram (1-byte type prefix + UTF-8 JSON body, base64-encoded).
- 14 request types (every `Msg` in `HANDLERS` with body shape + expected response).
- 10 notification types (every `Msg` pushed via `send_notification` from the backend, including the Slice 3.6 `COORDINATOR_STATE` + `COORDINATOR_EVENT` shapes mirrored from `proposals/05-coordinated-engine-architecture.md`).
- A 7-step checklist for adding a new message type without re-introducing drift.

A protocol-alignment Jest test in `frontend/__tests__/protocol-alignment.test.ts` now parses `constants.py` (`Msg` IntEnum + `HANDLERS` dict) as text and asserts the frontend `MESSAGE_TYPES` matches. **New drift fails CI** — the test runs in the Jest job from the CI workflow added today.

**Known documented drift:** `START_CLASSIC_PAIRING (0x66)` is declared in frontend `MESSAGE_TYPES` but has no entry in the backend `Msg` IntEnum or `HANDLERS` dict. The mobile can write a 0x66 message and the dispatcher will fall through to `_UNKNOWN_HANDLER`. Two resolution paths documented in `docs/BLE_PROTOCOL.md`:
1. Drop `START_CLASSIC_PAIRING` from the frontend if classic pairing isn't intended to be a BLE-triggered flow (BlueZ usually handles it through the agent layer, not via app messages).
2. Implement `handle_start_classic_pairing(...)` in `action_request_handlers.py` that accepts a target MAC and triggers BlueZ's pairing agent. Add `START_CLASSIC_PAIRING = 0x66` to `Msg`, register in `HANDLERS`, remove from the allowlist in the protocol-alignment test.

The protocol-alignment test allowlists this single drift so the test stays green while you decide; remove the allowlist entry as soon as the gap closes.

## 2026-05-14 — UTF-8 BLE encoder bug fixed + frontend has Jest tests

`frontend/utils/ble_codec.ts` `encode()` was sizing its Uint8Array by `json.length` (UTF-16 code units) but writing UTF-8 bytes into it. Any non-ASCII payload — speaker nicknames with emoji or accented characters, Japanese calibration phase labels in `CALIBRATION_RESULT` — threw `RangeError: offset is out of bounds`. Fix: size by `TextEncoder().encode(json).byteLength`, then write that array. Decoder was already byte-correct (used `TextDecoder` on the byte slice).

Backfilled the frontend's first Jest test suite at the same time: `frontend/__tests__/ble_codec.test.ts` with 11 tests covering encode wire format, decode happy + empty paths, full roundtrips (including the unicode regression case, base64-overlapping characters, and every `MESSAGE_TYPES` value used as the type byte).

Suite total: 15 frontend tests (11 codec + 4 protocol-alignment). All run in the new Jest CI job.

## 2026-05-14 — First CI for the repo + Pi-deploy verification baseline matches

`.github/workflows/ci.yml` was added today with two jobs:

- **Frontend** (`frontend/`): `npm ci` → `npm run lint` → `npx tsc --noEmit` → `npx jest --watchAll=false --passWithNoTests`.
- **Backend** (`backend/`): `pip install -r requirements.txt && pip install pytest` → `python -m compileall syncsonic_ble measurement` → `python -m pytest measurement -v`.

Triggers on push to `main`, `epic/**`, `feature-hardening`, `ui-polish`, and all PRs. Concurrency group cancels stale runs on the same ref. Pi hardware validation is **explicitly outside CI** per the AGENTS.md verification baseline — CI is the static / pure-logic surface only.

The CI matches the AGENTS.md "Verification Baseline" section exactly, so green CI is the same green an operator would see running the commands locally.

## 2026-05-14 — Live operational observations

Read-only audit on `syncsonic@10.0.0.89` (4-day Pi uptime, 10h service uptime, JBL Flip 6 + VIZIO SB2020n connected at 100% volume but no active stream — silent state). Captured here for future hardening work:

- **`virtual_out-74` PipeWire xrun cadence: ~10/24h.** Nine of ten xruns are on the delay-node sink; one was an upstream `alsa_input.usb-Jieli` xrun on the measurement mic input. Sparse (~1 every 2.4 hours) — not a sustained issue but worth a feature-hardening look. Concrete signal H1.
- **BT auto-disconnects: 4/24h.** Pattern of note: `A8:41:F4:F8:E1:18` disconnected → reconnected → disconnected within 24 seconds at 01:24 EDT. Flap pattern. The 2026-05-14 narrowing fix in `connection_manager.py` adds a `device_path is None → run_discovery` guard that should make the recovery path more predictable, but the underlying flap cause (RSSI dip? speaker timeout? router roaming?) is unresolved.
- **Service errors: 0** in the last 24 hours. `journalctl -u syncsonic.service -p err` returned no entries.
- **Disk usage: 6%** of 114 GB. Snapshot dir empty. H7 ("snapshot disk pressure") is not active.

## 2026-05-19 — Slice 0 of `ultrasonic-runtime-sync` shipped

Open-question experiment resolved. See [`epics/ultrasonic-runtime-sync.md`](epics/ultrasonic-runtime-sync.md) "Slice 0 Findings" for the conclusion + slice-1 architecture, and [`proposals/06-ultrasonic-vs-inband.md`](proposals/06-ultrasonic-vs-inband.md) for the raw experimental record.

## 2026-05-25 — Slice 1 v1 retired; redesign to in-filter burst emission (Option C)

Slice 1 of `ultrasonic-runtime-sync` shipped only the emission half (`backend/syncsonic_ble/helpers/arrival_burst_actuation.py` + BLE handler `handle_ultrasonic_sync` + ActuationManager hookup). Pi validation today on the **heyday** speaker (MAC `45:7A:D9:00:81:19`) surfaced two architectural failures with the chosen emission strategy.

### Phase 1: silent-room single-shot

One 100 ms 18.5 kHz burst emitted via `paplay --device=bluez_output.45_7A_D9_00_81_19.1` (exactly what `ArrivalBurstActuator.emit_once` does). Result:

- Burst visible at mic with **SNR ≈ 52 dB** — well above slice 0's 40 dB at the cheap unit; envelope-domain detection is comfortable on this speaker
- End-to-end latency emit → first arrival ≈ **325 ms**, emit → peak ≈ **375 ms**
- Clean rise / ~125 ms plateau / decay envelope — detector-friendly shape

### Phase 1b: 4-delay × 3-shot sweep with music playing

Filter delay swept across 0 / 100 / 250 / 500 ms (set via `backend/measurement/_filter_ctl.py set_delay`), 3 shots per delay value, music streaming through `virtual_out` throughout. 12 shots total.

**Findings (data straight, not editorialized):**

1. **Arrival time was indifferent to configured filter delay.** Mean peak time clustered at 358–375 ms across all four delay settings; in-shot variance ±25 ms (FFT hop resolution). Filter state confirmed at every step (`target_delay_samples = 0 / 4800 / 12000 / 24000`) — the filter accepted the commands but the bursts never went through it.

   Reason: `paplay --device=bluez_output.<mac>.1` writes directly to the BlueZ sink. The `pw_delay_filter` sits *between* `virtual_out` and `bluez_output` in the music path (see `backend/syncsonic_ble/helpers/pipewire_transport.py`). The probe path bypasses the filter entirely.

2. **Music played choppily during the sweep.** Operator-audible degradation throughout the test window. Two streams (filter-managed music + direct-to-sink burst) compete at the BT sink boundary — exactly the failure mode SyncSonic's architecture exists to prevent.

3. **Music does NOT mask the ultrasonic.** Noise floor only crept up ~1 dB (from −89.9 silent to −89.3 with music). SNR held at 60 ± 1 dB across all 12 shots regardless of music content. Confirms slice 0's finding that the 17.5–20 kHz band is naturally above music content.

4. **BT codec latency on heyday is rock-stable at ~370 ms.** Useful baseline number for the detector's window sizing.

### Decision: redesign slice 1 around in-filter burst emission (Option C)

The C `pw_delay_filter` process is already in the per-speaker audio path and is the only place where:

- Bursts can be timestamped frame-precisely against the same clock that processes music
- Bursts can be inserted into the speaker's audio stream without spawning a second concurrent stream
- Configured filter delay actually affects burst arrival time (so the loop can validate "delay setting → measured shift")
- The emission path matches the music path closely enough that measured latency is a meaningful end-to-end number for alignment correction

Cost: the filter's wire protocol needs extending — a "play this burst at the next zero crossing" command on the existing control socket, plus an event stream for frame-precise emit timestamps so the detector can compute arrival - emit.

**Slice 1 v1 emission code stays on its workstream branch (`maverick/syncsonic/ultrasonic/slice-1-cadence-based-ultrasonic-envelope-detector-drift-correction-loop-9437d092`) for reference but is NOT promoted into the epic branch.** A new slice 1 workstream targets Option C and replans from this finding.

### Theoretical caveat to revisit

In-filter burst emission means probe samples don't traverse the `virtual_out → filter input` PipeWire link that music traverses. Operator flagged this as a possible measurement-fidelity gap but expected impact is small relative to BT codec latency (PipeWire-internal hop is typically <5 ms vs ~370 ms codec). Worth measuring once the in-filter path exists; not a blocker for design adoption.

## 2026-05-26T01:11:27.909Z - Slice 1: cadence-based ultrasonic + envelope detector + drift correction loop

- Workstream: 9437d092-fa1e-4a06-9bd1-2ed6db2cd442
- Completed by: dashboard
- Epic: ultrasonic-runtime-sync
- Branch: maverick/syncsonic/ultrasonic/slice-1-cadence-based-ultrasonic-envelope-detector-drift-correction-loop-9437d092
- Summary: Your repeated instruction matches the implementation direction I’m on: no upstream proposal re-derivation, no muting/ducking, ActuationManager timing metadata, and per-sink `paplay` isolation. Local test execution is blocked by missing Python test dependencies, so I’m collecting the Pi-side read-only evidence next while keeping hardware validation clearly scoped as inspection only.

The Pi is reachable, but the service is currently inactive, so I’m not claiming live hardware playback validati...
- Planned slice: Dispatch the structured plan to Codex for implementation
- Latest turn: Your repeated instruction matches the implementation direction I’m on: no upstream proposal re-derivation, no muting/ducking, ActuationManager timing metadata, and per-sink `paplay` isolation. Local test execution is blocked by missing Python test dependencies, so I’m collecting the Pi-side read-only evidence next while keeping hardware validation clearly scoped as inspection only.

The Pi is reachable, but the service is currently inactive, so I’m not claiming live hardware playback validati...
- Last next action: Verify the changes before moving to review.

## 2026-05-26T01:28:35.658Z - Slice 0: ultrasonic vs in-band probe measurement

- Workstream: 531fcb61-da79-459c-ae92-786bed9c9511
- Completed by: dashboard
- Epic: ultrasonic-runtime-sync
- Branch: maverick/syncsonic/ultrasonic/slice-0-ultrasonic-vs-in-band-probe-measurement-531fcb61
- Summary: Planning produced a final Codex execution prompt.
- Planned slice: Dispatch the structured plan
- Latest turn: Codex App Server adapter shutting down
- Last next action: Inspect the failed turn output and decide whether to retry or steer the workstream.

## 2026-05-26 — Slice 1 (Option C) shipped + validated

PR [#18](https://github.com/BrooksWimer/SyncSonicRaspberry/pull/18) merged into the `ultrasonic-runtime-sync` epic branch as squash commit `7314834`. Filter-resident ultrasonic burst emission is live on the Pi and produced a clean Pi-validated 24000-sample frame_index delta (= exactly 500 ms × 48 samples/ms) when the configured filter delay was shifted from 113 ms to 613 ms. Heyday's alignment was restored to baseline (`target_delay_samples=5424`) before the workstream archived.

### What's on the epic branch now

- C filter (`backend/tools/pw_delay_filter.c`): burst queue, delayed in-filter burst synthesis, emit timestamp ring, `emit_burst` socket command, `query_emit_timestamps` socket command, `burst_active` query field
- Python actuation (`backend/syncsonic_ble/helpers/arrival_burst_actuation.py`): thin socket-command wrapper, no direct paplay
- Service transport (`backend/syncsonic_ble/helpers/pipewire_transport.py`): two new wrapper methods + `-lm` link flag for the audio synthesis math
- BLE handler `handle_ultrasonic_sync` un-stubbed (still single-speaker, no detector)
- Manual harness extended (`backend/measurement/_filter_ctl.py emit_burst` and `query_emit_timestamps` subcommands)
- Service startup script (`backend/start_syncsonic.sh`): `-lm` link flag in the auto-rebuild path so systemd recovery doesn't break on `sinf`/`cosf`

### What's still NOT in the application

Slice 1 only delivered the emission half + a manual validation harness. The integrated drift-correction loop the epic was built for does not yet exist:

- **No detector.** Mic audio is not yet analyzed for burst arrival timestamps. The `frame_index` from emit_log + a mic capture timestamp = per-speaker latency, but the consumer of that math isn't written.
- **No continuous cadence.** Bursts are issued manually one at a time. The "~1 Hz cadence per active speaker" loop from the epic charter is not running.
- **No drift feedback.** Measured latencies are not yet fed into the elastic engine's `set_rate_ppm` for bounded rate correction.
- **No multi-speaker disambiguation.** Single speaker only. The 18.0 / 18.5 / 19.0 / 19.5 kHz frequency rotation idea from the proposal is unimplemented.
- **No UX surface.** No on/off toggle in `SpeakerConfigScreen.tsx`, no "drift correction: on" status pill, no per-speaker correction visualizer.
- **No soak validation.** 24-hour music session with varied speaker mixes hasn't been run.

### Epic promotion status

Epic branch is now 3 commits ahead of `main` (slice 0 squash, redirect-decision doc, slice 1 squash). **Stays unmerged.** Promotion to main waits until the full drift-correction loop is shipping end-to-end and a soak session under varied music holds alignment within audible threshold without operator intervention — that's the epic's original success signal and slice 1 is the first of several toward it.

### Workstream cleanup

Workstream `4307e4eb-58a0-46d0-80b3-e0b6cb26e4dc` archived. Pi switched cleanly to the `ultrasonic-runtime-sync` epic branch, deploy artifacts stashed for reference (`pi-deploy-artifacts-2026-05-25-slice1-optionC`). Pi-side stale v1 test file `measurement/test_arrival_burst_actuation.py` removed (it tested APIs Option C deleted). Heyday speaker delay still at 5424 samples (113 ms) per pre-test alignment.

## 2026-05-26 — Slice 2 dispatched: open-loop latency measurement (2-speaker scope)

Slice 2 = measure-but-don't-correct. The closed-loop correction (feeding measurements into the elastic engine's `set_rate_ppm`) is deferred to slice 3 so we can watch real per-speaker latency stabilize on hardware before letting it drive anything.

**Scope decided:**
- **Two speakers only for now** (down from three per epic-charter language). Operator reported something off with the third speaker; investigating that is its own work, not in this slice.
- **15-second cadence per speaker** → 30-second full cycle for two speakers → ~20 measurements per speaker in a 10-minute run.
- **Detector lives in Python on Pi** (per the use-fast-iterate principle — we measure CPU cost and rewrite to C only if profiling demands).
- **Wall-clock alignment** between mic capture timestamps and `emit_burst` return time. Acceptable for slice 2's slider-correlation experiment (slider moves are 100s of ms; wall-clock-to-audio-clock drift is ~10 ms over the experiment). Audio-clock alignment is deferred to slice 3 where the drift signal itself is ~µs/s.
- **`frame_index_emitted` is also logged per emit** even though slice 2 doesn't compute against it — gives slice 3 a clean swap-in for tighter alignment.
- **Context-aware mic window:** `expected_arrival = t_emit + filter_delay_depth + estimated_BT_codec_latency`; initial margin 250 ms; tighten to 100 ms after 5+ stable measurements per speaker.
- **5-second warmup** before the first burst — establishes mic noise floor used as detector threshold for the rest of the run.
- **Slider value logged at each emit:** the service queries the filter's `target_delay_samples` immediately before each `emit_burst` and logs it alongside emit + arrival timestamps.
- **Log to systemd journal** (`syncsonic.service` log). Operator inspects via `journalctl -u syncsonic.service -f` over SSH.
- **Missed bursts** (detector finds no peak in window) are logged as a separate event — diagnostic signal for BT dropouts, mic issues, etc.
- **Manual start/stop** for slice 2 (CLI on Pi). BLE handler / automatic-on-music wiring is slice 3 work.

**Out of scope for slice 2:** any modification to the elastic engine, any `set_rate_ppm` calls, any UX surface, multi-speaker disambiguation via frequency rotation (single-speaker-at-a-time time separation does the disambiguation for 15-s cadence), soak validation.

Slice 2 success criterion: after a 10-minute run with operator manually moving filter delay sliders mid-run, the journal log lets us reconstruct per-speaker latency over time AND we can see that slider moves correlate with corresponding shifts in measured arrival. That correlation IS the proof that end-to-end measurement works.

Dispatched as a new Maverick workstream targeting the `ultrasonic-runtime-sync` epic branch.


## 2026-05-27 — Slice 2 implementation cleanup: align with design scope

The initial slice 2 Codex commit (`e702795`) over-scoped by adding a systemd unit file, a separate CLI shim with Unix-socket IPC, and placing the main service in `backend/syncsonic_ble/` instead of `backend/measurement/`. Operator did a direct-branch cleanup pass (no new Codex turn) to align with the resolved slice 2 scope.

**What changed in cleanup:**
- Moved `backend/syncsonic_ble/runtime_sync_service.py` → `backend/measurement/runtime_latency_service.py` (canonical path for measurement scripts; matches slice 0 + slice 1 convention).
- Deleted `backend/runtime-sync` (the `runtime-sync ctl <cmd>` IPC shim — out of scope; slice 2 is manual-start CLI only).
- Deleted `backend/syncsonic-runtime-sync.service` (systemd unit — out of scope; manual invocation only).
- Stripped the Unix-socket control plane from the main service file: removed `CONTROL_SOCKET` constant, `_start_control_socket`, `_handle_control`, `_dispatch_control` methods, the `self.server` attribute, the `--auto-start` CLI arg, and the conditional auto-start path in `run()`. Service now always begins measurement after capture initialization and shuts down cleanly on SIGINT/SIGTERM via the existing `stop_event` handler. Net delta: 71 lines removed, file went from 566 → 495 lines.

**What survived (the design-compliant core):**
- Long-running `parecord` subprocess piped into in-memory `RingBuffer` ✓
- `EnvelopeDetector` with 5-second warmup baseline, sliding-window FFT (50 ms window / 25 ms hop / 17.5–20 kHz band, hanning + RMS comp) ✓
- Per-burst measurement cycle: query filter delay → `emit_burst` → `query_emit_timestamps` → context-aware window → detect peak → compute latency_ms → JSON-lines log ✓
- Context-aware window: 250 ms margin per speaker, tightens to 100 ms after 5+ stable measurements; per-speaker rolling estimated_codec_latency starting at 370 ms ✓
- Filter-socket auto-discovery intersected with `backend/syncsonic_ble/helpers/adapter_helpers.py:connected_devices_on_adapter()`, graceful try/except ✓
- Inline `_send_filter_command(socket_path, payload)` helper bypassing `PipeWireTransportManager` ✓
- JSON-lines stdout via `print(json.dumps(record), flush=True)`, operator invokes via `python3 backend/measurement/runtime_latency_service.py [args]` or wrapped under `systemd-cat -t runtime-latency ...` for journald routing ✓
- CLI flags: `--mic-source`, `--mic-source-prefix`, `--cadence-sec`, `--warmup-sec`, `--max-speakers`, `--freq-hz`, `--duration-ms`, `--amplitude`, `--bt-codec-latency-ms` ✓
- 2-speaker default (`--max-speakers 2`), per operator scope ✓

**Verification:**
- `python3 -m py_compile backend/measurement/runtime_latency_service.py` passes
- Pi hardware validation (10-minute slider-correlation run) NOT done yet — that's the next step before merging the workstream branch into the epic.

**Process notes:**
- The over-scope happened because the original planner's `finalExecutionPrompt` included the systemd-unit + CLI-shim requirements, and a subsequent operator `reset-to-planning` action auto-pipelined into implementation without picking up the operator's corrective instruction (see Maverick Tier 2 use case log).
- Direct manual cleanup chosen over re-dispatching Codex because the changes are mechanical (file move + delete-extra-files + strip ~71 lines) and re-running the orchestrator risks repeating the same auto-dispatch issue.

## 2026-05-27T02:53:06.509Z - Slice 2: open-loop per-speaker latency measurement

- Workstream: 130e247e-6226-41a0-b749-f24afb62b652
- Completed by: dashboard
- Epic: ultrasonic-runtime-sync
- Branch: maverick/syncsonic/ultrasonic/slice-2-open-loop-per-speaker-latency-measurement-130e247e
- Summary: I’ll first orient against the current branch docs and existing backend utilities, then I’ll make the service changes in this worktree only. Since this touches runtime ultrasonic measurement and service startup, I’ll treat Pi validation as relevant and separate local verification from any remote evidence.

The sandbox failed before any command executed, so I requested and received permission for read-only inspection. I found the ultrasonic runtime epic doc and the standard verification skill d...
- Latest turn: I’ll first orient against the current branch docs and existing backend utilities, then I’ll make the service changes in this worktree only. Since this touches runtime ultrasonic measurement and service startup, I’ll treat Pi validation as relevant and separate local verification from any remote evidence.

The sandbox failed before any command executed, so I requested and received permission for read-only inspection. I found the ultrasonic runtime epic doc and the standard verification skill d...
- Last next action: Verify the changes before moving to review.

## 2026-05-27 — Slice 2 shipped + slice 3 dispatched

Slice 2 (open-loop per-speaker latency measurement) merged into the epic as squash commit `1768cc1` via PR [#20](https://github.com/BrooksWimer/SyncSonicRaspberry/pull/20). Pi validation on the operator's 2-speaker setup confirmed end-to-end measurement works: 38/38 bursts detected, slider-to-latency slope = 1.004 and 1.009 (perfect tracking = 1.000), zero missed bursts across slider values 131-1800 ms.

**Per-speaker intrinsic codec latency now measurable (continuous):**
- `28:FA:19:B6:0E:3B`: 349 ms ± 22 ms, mean SNR 41.7 dB
- `F4:6A:DD:D4:F3:C8`: 418 ms ± 34 ms, mean SNR 20.2 dB

These two numbers are the new sensor signal slice 3 consumes. The 22-34 ms stdev is mostly FFT-hop quantization (25 ms hop); the true codec-clock drift component is smaller and only emerges over longer time horizons.

### Slice 3 dispatch: closed-loop drift correction during music playback

**Decision: extend the slice 2 service in-place.** Add a controller that consumes the existing per-burst latency stream and feeds bounded `set_rate_ppm` adjustments back into the elastic delay engine. Same process, same measurement loop — just an optional new code path. No new service, no two-process coordination, no IPC.

**Resolved design forks for slice 3 (do NOT re-derive in planning):**

- **Architecture: extend `backend/measurement/runtime_latency_service.py` in-place.** The slice 2 service already has the per-burst measurement loop, the per-speaker rolling state, and the JSON-lines logging. Slice 3 adds a `DriftController` class and a `--enable-correction` CLI flag. Default behavior unchanged (measure-only) so existing invocations keep working; correction is opt-in for slice 3.

- **Controller algorithm: simple proportional control with low-pass smoothing.** Maintain a rolling baseline of (latency_ms - slider_ms) per speaker = intrinsic codec latency. Detect drift as the deviation of the recent rolling mean from the long-term baseline. Apply a small `set_rate_ppm` correction proportional to that deviation. Bounded at ±50 ppm per ROADMAP §4. Tunable smoothing window (default 5 measurements rolling).

- **Confidence gating: require N=5 consecutive measurements per speaker before applying any correction.** SNR must stay above 10 dB and stable_count must be ≥ 5 for that speaker. If either fails, skip the correction cycle for that speaker but keep measuring. Logged as `correction_skipped` with the reason.

- **Operator escape:** the `--enable-correction` flag is the on/off switch at startup; mid-run disable is via `sudo systemctl stop runtime-latency` (the slice 2 SIGINT/SIGTERM path) — there is no separate runtime toggle for slice 3, and that is intentional (slice 3 is single-binary, slice 4+ can add an in-band BLE toggle if the operator hits a reason to want one).

- **Auto-disable safety:** if any speaker accumulates 3+ consecutive `correction_skipped` events (low confidence) AND its rolling SNR drops below 10 dB, the controller stops issuing corrections for THAT speaker (logged as `controller_paused`). The measurement loop continues. Other speakers are unaffected.

- **Clock alignment:** keep slice 2's wall-clock alignment for the FIRST cut of slice 3. The ±25 ms FFT-hop quantization is the dominant noise term right now; audio-clock alignment would only help when the controller becomes responsive enough that wall-clock jitter matters. Document this as a known limitation in the slice 3 implementation, revisit if controller tuning surfaces wall-clock as the bottleneck.

- **2-speaker scope** stays. The architecture should NOT bake in "exactly two" — the controller logic should work for N speakers — but the validation runs and tuning happen on the operator's current 2-speaker rig.

- **Logging: extend slice 2's JSON-lines schema.** Add `correction_proposed`, `correction_applied`, `correction_skipped`, `controller_paused` event kinds. Each carries: speaker_mac, baseline_codec_ms, current_codec_ms, drift_ppm_estimated, applied_ppm, reason (for skip/pause). Operator inspects via `journalctl -u runtime-latency -f` just like slice 2.

- **Set-rate-ppm pathway:** the elastic engine already accepts `rate_ppm` corrections via the same filter socket protocol (`_send_filter_command(socket_path, "set_rate_ppm <ppm>")` per the existing filter wire format). Slice 3's controller uses the same `_send_filter_command` helper slice 2 added; no new transport.

**Validation criterion for slice 3:** a 30-minute music session with the controller enabled, both speakers playing, no operator intervention. At the end, the journal log should show: (a) corrections applied within ±50 ppm bound, (b) measured per-speaker latency staying within ±10 ms of its starting baseline (drift contained), (c) zero `controller_paused` events on a healthy system. If `controller_paused` fires during the session, that is a real signal worth investigating (audio dropout, BT disconnect, etc.) — slice 3 should make those events easy to read in the journal.

**Out of scope (do NOT include in slice 3):**
- UX surface (the "drift correction: on" status pill in `SpeakerConfigScreen.tsx` is slice 4+)
- BLE in-band on/off toggle (slice 4+)
- Multi-speaker disambiguation via frequency rotation (single-speaker-at-a-time time separation per slice 2 still applies)
- Soak validation beyond 30 min (24-hour soak is slice 4+)
- Audio-clock alignment refactor (deferred; revisit if wall-clock jitter becomes the bottleneck)
- Third-speaker scaling investigation (the operator-deferred work from earlier)
- Any systemd unit file, separate CLI shim, or IPC (slice 2 cleanup lesson — these are operator invocation concerns, not slice deliverables)

**File constraints (so the planner doesn't invent paths):**
- Modify ONLY `backend/measurement/runtime_latency_service.py` (extend in-place)
- May add helper functions/classes to the same file
- Do NOT create new directories (`src/`, `deploy/`, etc. don't exist in this repo)
- Do NOT create separate CLI shims, systemd unit files, or IPC sockets
- Do NOT modify `pyproject.toml` (not in active use)
- Reference existing `_send_filter_command` helper (slice 2) for filter socket calls
- Reference existing `EnvelopeDetector` / `RingBuffer` / discovery code (slice 2) — leave untouched

Targets `ultrasonic-runtime-sync` epic branch. Epic stays unmerged until slice 4+ soak validation per the epic-promotion gate.

## 2026-05-27 — Slice 3 implementation spec doc added (planner pointer)

The slice 3 planning agent should template the implementation plan directly from [`proposals/09-slice-3-implementation-spec.md`](proposals/09-slice-3-implementation-spec.md) (committed as `030c348` on this epic branch). That doc has the exact class structure (`DriftController`), method signatures (`observe`), integration points (`RuntimeSyncService.__init__` and `_measure_once`), the three CLI flags (`--enable-correction`, `--max-ppm`, `--smoothing-window`), and the five log event kinds with payload fields. Treat the spec doc as a templating reference, not a starting point for re-derivation. The prior planning round mis-named the existing service class (`RuntimeLatencyService` vs. the actual `RuntimeSyncService`) and omitted the per-speaker state model entirely; the spec doc fixes both, so the next planning pass should consult it before writing implementation steps.
