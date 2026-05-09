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
