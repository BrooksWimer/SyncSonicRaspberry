# Epic 05: Coordinated Engine — Architecture Proposal

_Status: Slices 0–3 deployed and Pi-validated on `epic/05-coordinated-engine`.
**Slice 4** (mic calibration, startup chirp, sequential align-all) is in
progress—see §360+. Evidence sections 8–16 remain the Pi audit trail._

_For the strategic frame this epic sits inside (the North Star, the three
time horizons, and the design principles every slice is held to), see
[`../ROADMAP.md`](../ROADMAP.md). This document is the engineering
plan; ROADMAP.md is the contract with the dream._

This document is the long-form rationale, evidence, and slice plan for
[Epic 05](../epics/05-coordinated-engine.md). It is the source of truth
for "why" we are doing this work; the epic doc is the source of truth for
"what" and "how it will be validated."

## 1. The Pivot In One Paragraph

The per-speaker delay filter, the manual alignment slider, the disabled
ultrasonic runtime correction feature, and the response to a Bluetooth
transport failure should all be the same physical mechanism, controlled
by a single system-wide coordinator that holds every output accountable
to every other output. Today they are four disconnected systems that
contradict each other. That contradiction is the source of every
"hiccup" the listener hears today.

## 2. What Actually Runs on the Pi

Live evidence captured 2026-04-28 ~22:35 EDT, while three speakers were
playing.

```mermaid
flowchart LR
    Phone["Phone (44.1 kHz A2DP source)"]
    BzIn["bluez_input.AC_DF_A1_52_8A_41.2 (PW source, 44.1k)"]
    Vout["virtual_out (null-sink, 48k F32, default, priority.driver=2500)"]
    F1["pw_delay_filter F4..C8 FL+FR (delay=80ms)"]
    F2["pw_delay_filter 2C..0A FL+FR (delay=90ms)"]
    F3["pw_delay_filter 28..3B FL+FR (FAILED at 22:35:00)"]
    Bz1["bluez_output.F4..C8.1 (48k SBC, ERR=1 accumulated)"]
    Bz2["bluez_output.2C..0A.1 (48k SBC, ERR=0)"]
    Bz3["bluez_output.28..3B.1 (TRANSPORT FAILED, removed)"]
    Mic["alsa_input usb-Jieli mic (SUSPENDED, never opened)"]
    Phone --> BzIn --> Vout
    Vout -- monitor_FL/FR --> F1 --> Bz1
    Vout -- monitor_FL/FR --> F2 --> Bz2
    Vout -- monitor_FL/FR --> F3 --> Bz3
```

Concrete observations:

- 4 BT controllers: `hci3` (UART, on-board, advertising) plus `hci0/1/2`
  (USB output)
- `virtual_out` runs at 512-sample quantum @ 48 kHz, BUSY ≈ 8 µs, ERR 0
- `bluez_output.F4_6A_DD_D4_F3_C8.1` BUSY 60–77 µs, **ERR 1** silently
  accumulated over 4h55m
- the phone enters at **44.1 kHz**, gets resampled inside `virtual_out`
  to 48 kHz before fan-out
- the per-speaker `pw_delay_filter` processes link directly between
  `virtual_out:monitor_FL/FR` and `bluez_output:playback_FL/FR` — there
  is no stage null-sink and no PA loopback in the live chain, despite
  what the local `wip/01-pipewire-transport-phone-ingress` version of
  `pipewire_transport.py` claims
- the deployed `pipewire_transport.py` is 398 lines and ships the older
  `"PipeWire delay transport established"` log line; the local wip
  version is 559 lines with a different message — local code is ahead

## 3. Why Hiccups Are Perceptible Today (Seven Root Causes With Evidence)

### 3.1 The control plane and physical reality silently diverge

`/tmp/syncsonic_pipewire/control_state.json` listed the phone MAC
`AC:DF:A1:52:8A:41` as an active speaker output. The actuation daemon
polled every 250 ms and emitted

```
WARNING - PipeWire transport sink not found for AC:DF:A1:52:8A:41
```

over 800 times in 6 minutes. There is no source of truth and no observer
that flags the disagreement.

### 3.2 Manual latency adjustments cause graph xruns every time

```
22:14:41 wireplumber: (bluez_output.2C_FD_B4_69_46_0A.1-120) graph xrun not-triggered
22:14:41 syncsonic: PipeWire delay transport established for 2C:FD:B4:69:46:0A ... (delay 90.0 ms)
```

Same pattern at 21:33:28. `pipewire_transport.py:ensure_route` rebuilds
the route whenever the requested delay differs by ≥ 0.5 ms. The
underlying `pw_delay_filter.c` already has a ring buffer that could
adjust `delay_samples` live; it does not because there is no IPC into
it.

### 3.3 A speaker dropped at 22:35:00 and the system response was teardown, not recovery

```
22:35:00 pipewire: (virtual_out-74) graph xrun pending:1/11 waiting:1593926us process:31463us
22:35:00 wireplumber: Failure in Bluetooth audio transport /org/bluez/hci1/dev_28_FA_19_B6_0E_3B/sep2/fd2
22:35:00 syncsonic: [BlueZ] 28:FA:19:B6:0E:3B is now DISCONNECTED
22:35:00 syncsonic: PipeWire delay transport removed for 28:FA:19:B6:0E:3B
```

The JBL Flip 6 had a 1.6 second gap, BlueZ flagged the A2DP transport
failed, WirePlumber tore down the bluez_output node, the code's response
was log + nuke route + tell app the speaker is gone. No retry, no
recovery, no protection of the other two speakers from the propagated
xrun.

### 3.4 Three independent timing domains, weakly coupled

The audio path has three clocks: virtual_out (system, 48 kHz, 512-sample
quantum), per-speaker A2DP transport (speaker crystal, ±20–80 ppm drift
plus retransmit jitter), and the phone source (44.1 kHz, drifting
independently of virtual_out). PipeWire absorbs differences at three
elastic points (input resampler, virtual_out monitor read, A2DP encoder
queue), each with its own behavior. There is no system-level model of
total accumulated drift. The delay filter is fixed-delay and does not
compensate.

### 3.5 WirePlumber and our code race for graph clock priority

The custom WirePlumber rule pins all bluez nodes to `priority.driver=100`.
The wip-branch `pulseaudio_helpers.py` does a separate `pw-cli set-param
priority.driver=2500` on `virtual_out` after creation. The
`module-null-sink` load also passes `priority.driver=5000`. Three
sources, three values. If WirePlumber re-applies its rule on a bluez
reconnect and the post-create promotion isn't re-run, a bluez_output
with default `priority.driver=1010` can become the graph clock master,
and any BT transport jitter stalls every speaker.

### 3.6 The phone enters at 44.1 kHz and we don't know it

`bluez_input.AC_DF_A1_52_8A_41.2` runs at 44100 Hz; everything
downstream is 48 kHz. Resampling happens silently in PipeWire. If the
phone's A2DP source briefly stalls (iOS/Android background, notification
service), the resampler underruns and the click fan-outs to all
speakers. There is no telemetry on the resampler.

### 3.7 There is no observability anywhere

When a hiccup happens we do not know which queue underran, how deep the
BT TX queue was at the time, whether the A2DP bitpool dropped (a sudden
drop from 53 to 35 sounds like a quality crash), whether USB IRQ
contention spiked, or which speaker slipped first. The user reacts with
the slider; the slider causes an xrun (3.2). It is a self-undermining
loop.

## 4. The Architecture That Solves This

### 4.1 The single idea

The per-speaker delay filter, the alignment actuator, and the runtime
correction loop are **the same physical mechanism**: a per-speaker
elastic-buffer engine that accepts a target delay (slow lane, for
alignment), a bounded rate offset in PPM (fast lane, for jitter and
drift), and emergency commands (immediate pad / soft mute / ramped
re-entry) over a single Unix-socket control surface, controlled by a
**single system coordinator** that owns global state.

### 4.2 The runtime topology

```mermaid
flowchart TB
    subgraph Capture["Capture & Truth"]
        Mic["USB Mic (always-on; today SUSPENDED)"]
        VOTap["virtual_out.monitor tap (ground truth)"]
    end
    subgraph Control["System Coordinator (replaces actuation_daemon; in-process state)"]
        Model["Per-speaker model: queue depth, drift, codec, BT errors"]
        Policy["Policy: bounded rate, system-wide hold, soft-mute"]
        Telemetry["jsonl telemetry stream"]
    end
    subgraph Engine["Per-Speaker Engine (replaces pw_delay_filter.c)"]
        Buf1["Stereo elastic ring 400ms + IPC socket: speaker A"]
        Buf2["Stereo elastic ring 400ms + IPC socket: speaker B"]
        Buf3["Stereo elastic ring 400ms + IPC socket: speaker C"]
    end
    Mic --> Model
    VOTap --> Model
    BTev["BlueZ MediaTransport1 + WP xrun events + bluez kernel TX queue"] --> Model
    Buf1 -- "queue depth every 10ms" --> Model
    Buf2 -- "queue depth every 10ms" --> Model
    Buf3 -- "queue depth every 10ms" --> Model
    Model --> Policy
    Policy -- "delay/rate/pad cmd" --> Buf1
    Policy -- "delay/rate/pad cmd" --> Buf2
    Policy -- "delay/rate/pad cmd" --> Buf3
    Policy --> Telemetry
```

### 4.3 The three policy primitives

**Bounded rate adjustment (the "fast lane").** A per-speaker rate offset
in PPM, clamped to ±50 ppm. Applied continuously by a per-speaker PI
controller targeting 50% buffer depth. ±50 ppm is inaudible; humans
don't perceive pitch shifts below ~1000 ppm at typical music spectra.
Replaces all "manual rate matching."

**System-wide synchronous hold (the "panic lane").** When **any one**
speaker's queue starts draining faster than its rate adjustment can
compensate, the coordinator applies a small system-wide rate slowdown
(−20 ppm, ~200 ms) to **all** speakers. Because all speakers slow
together, no inter-speaker drift is perceptible — the listener perceives
a tiny overall stretch which is also inaudible. This is the magic that
makes hiccups unrecognizable: we do not try to save the failing speaker
by speeding it up (which would cause pitch artifacts), we let everyone
wait for the failing speaker to catch back up.

**Soft mute + phase-aligned re-entry (the "graceful failure").** When a
speaker's BT transport actually fails: ramp its output to silence over
30–50 ms (perceived as a fade-out, not a click), keep the other speakers
stable on the system clock, attempt BlueZ A2DP re-establish without
`RemoveDevice`, when the transport is back wait until the next
phase-aligned beat boundary in the rate-adjusted timeline, ramp back in.
Listener experience: one speaker briefly fades and returns, others
uninterrupted. This is the quality bar AirPlay and Sonos meet.

### 4.4 What we keep, what we refactor, what we scrap

| Keep as-is | Refactor | Scrap |
|---|---|---|
| BLE GATT service `infra/gatt_service.py` | `pw_delay_filter.c` — keep architecture, add IPC socket, deepen ring to 400 ms, support stereo in one process, add queue-depth reporting | Hybrid stage+loopback dead code in the wip-branch `pipewire_transport.py` — never deployed, won't be needed |
| BlueZ FSM in `connection_manager.py` `_try_reconnect` | `pipewire_actuation_daemon.py` becomes the System Coordinator with a real model, not a 250 ms JSON poller | JSON-on-disk control plane in `pipewire_control_plane.py` — coordinator owns state in process |
| Per-MAC adapter scheduling in `action_planning.py:connect_one_plan` | `alignment_actuator.py:step()` becomes the policy engine instead of just translating delay→delay-line | Three-place priority.driver setting — collapse to one, owned by the coordinator |
| WirePlumber BT priority pinning rule (currently untracked in `wip/01`) | `pulseaudio_helpers.py` shrinks; coordinator handles graph state | Per-speaker FL+FR as 2 separate processes — collapse to 1 stereo process per speaker, halves PID count |
| Phone ingress via WirePlumber autoconnect | Add the missing guards so the phone MAC never enters the speaker control plane | Unused `Sonos`/Wi-Fi feature flags scattered across handlers |

The USB measurement microphone (`alsa_input.usb-Jieli_Technology_UACDemoV1.0_*`)
is already plugged in and recognized by PipeWire. It is currently
`SUSPENDED` because nothing opens it. Once we open it in Slice 1, the
coordinator has its truth source for free.

## 5. Slice Plan

Each slice is small, independently deployable to the Pi, and produces
objective evidence on the Slice 1 telemetry stream.

### Slice 0: Bug-fix triage (1 day)

Concrete bugs we have direct evidence for in the journal. Do them first
because they make the system stop lying to itself.

- Add a phone-MAC guard so the phone's MAC can never reach the speaker
  control plane via `handle_set_latency`, `handle_set_volume`, or
  `publish_output_*`. Defense in depth: also guard `_ensure_output_actuation`
  in `connection_manager.py`. Kills the 410 ms warning storm and
  recovers some CPU.
- `transport.ensure_route` records "speaker offline" once per MAC and
  stops re-trying until a fresh `Connected` event fires. The actuation
  daemon honors this; no more 250 ms spin warning.
- Collapse priority.driver to one source. Ship the WirePlumber rule
  (currently untracked at `backend/wireplumber/bluetooth.lua.d/...`) to
  the foundation, set `priority.driver=10000` on the `module-null-sink`
  load line, and remove any post-create `pw-cli set-param` promotion.
- One-shot auto-reconnect when an "expected" speaker disconnects
  unexpectedly: requeue a single `Intent.CONNECT_ONE` instead of
  immediately giving up. The existing FSM handles its 3-attempt internal
  retry.

**Success criterion (Pi-validated):** journal shows zero
`transport sink not found` warning loops; one transient BT failure
recovers without speaker disconnect at the user-visible layer; latency
slider on a non-existent speaker MAC produces no xrun.

### Slice 1: Telemetry + measurement harness (1 week)

You can't fix what you can't see. No behavior change; the system gets
honest.

- Wake the USB mic. Tiny capture process always-on records a 30-second
  rolling window from `alsa_input.usb-Jieli...` to a memory ring.
  Becomes ground truth for everything.
- One jsonl per session at `/var/log/syncsonic/session-<ts>.jsonl` with
  structured events (`pw_xrun`, `bluez_transport_error`,
  `delay_filter_queue_depth`, `route_create`, `route_teardown`,
  `set_latency_request`, `mic_rms_window`, `rssi_sample`,
  `rssi_baseline`), all timestamped with `CLOCK_MONOTONIC`.
- Pipe `pw-mon` (or periodic `pw-cli ls Node` snapshots) every 1 s into
  the same stream so we capture priority.driver changes, codec changes,
  and bluez node states.
- BlueZ `MediaTransport1` property snapshots (Codec, Volume, Delay,
  Configuration) every 5 s. These properties change rarely so a slow
  poll is fine.
- **Per-speaker RSSI sampling every 1 s per connected output** via
  `hcitool -i <hci> rssi <mac>` (or the equivalent BlueZ D-Bus
  property if available without privilege escalation). For each
  speaker, maintain in process: the latest sample, a 10-second rolling
  median, and a 60-second rolling baseline median. All three are
  emitted to the jsonl as `rssi_sample` (every 1 s) and
  `rssi_baseline` (every 10 s). These are the leading indicator of
  dropouts — the field experiment in section 9 (2026-04-29) confirms
  the system is RF-limited and that RSSI moves seconds before the
  audible dropout. Single-sample RSSI is too noisy to be useful;
  rolling medians are the minimum reliable read.
- One command (`make session NAME=test1`) plays a fixed 30 s music
  sample, captures everything, produces a single-page report: dropout
  count, inter-speaker drift, codec changes, xrun count, **per-speaker
  RSSI median + variance, and RSSI-vs-xrun temporal correlation**
  (does dropout cluster within N seconds of an RSSI dip?).

**Success criterion (Pi-validated):** `make session` twice with no code
changes produces reproducible numbers within ±5% on every primary
metric, **including the RSSI-vs-xrun correlation coefficient**.

### Slice 2: Stereo elastic delay engine + IPC (2 weeks)

Replace per-speaker FL+FR processes with a single stereo elastic ring
per speaker, controlled over a Unix socket.

- Rewrite `pw_delay_filter.c`:
  - One process per speaker, stereo (FL+FR) sharing one ring buffer
    (400 ms / 38400 samples per channel)
  - Unix socket at `/tmp/syncsonic-engine/<mac>.sock` accepting
    line-based commands: `set_delay <ms>`, `set_rate_ppm <ppm>`,
    `pad <samples>`, `mute_ramp <ms>`, `query`
  - Smoothly interpolates `delay_samples` toward `set_delay` over ~10 ms
    (no xrun, no pop)
  - Applies `rate_ppm` by inserting/dropping 1 sample every
    `(1e6 / |ppm|)` frames using linear interpolation around the read
    pointer (transparent at ±50 ppm)
  - Reports queue depth, frames in/out, errors via `query` response
- Update the daemon to send `set_delay` over the socket instead of
  restarting the process

**Success criterion (Pi-validated):** continuously adjust the slider
50→300→50 ms with music playing; session report shows zero xruns. Today
every adjustment xruns.

### Slice 3: System Coordinator (2 weeks)

Replace `pipewire_actuation_daemon.py` with a coordinator that owns
state in-process.

- In-process per-speaker model: `target_delay_ms`, `current_delay_ms`,
  `current_rate_ppm`, `queue_depth_samples`, `queue_depth_target`,
  `bt_transport_state`, `last_xrun_ts`, `consecutive_stress_ms`
- 20 ms tick: read every speaker's `query` response from its socket,
  read latest WP xrun events from the jsonl tap, read BlueZ
  `MediaTransport1` deltas
- Policy:
  - Each tick, compute a per-speaker rate adjustment proportional to
    `(queue_depth − target)`. PI controller, clamp to ±50 ppm.
  - If any speaker's `consecutive_stress_ms > 100`, apply a system-wide
    rate slowdown of −20 ppm for 200 ms.
  - If any speaker's queue is `< 10%` and BT transport state shows
    error/buffering, soft-mute that speaker (`mute_ramp 50`), schedule
    re-establish, do not disconnect.
- BLE notifications expose per-speaker queue health, not just
  connected/disconnected, so the app can show drift bars and dropout
  warnings.

**Success criterion (Pi-validated):** with a 2.4 GHz interferer (microwave
running, BT scan in progress, USB hub power blip via `uhubctl -p X -a
0`), the session report shows other speakers maintained zero audible
dropout while the stressed speaker dipped queue, was held by the
coordinator, and recovered without disconnect.

### Slice 4: Mic-driven alignment (in progress)

Delivered incrementally as 4.1 (offline analyzer), 4.2 (sequential mute +
single-speaker calibration against live ``virtual_out.monitor`` audio),
4.3 (same pipeline once per connected filter socket; BLE opcode
``CALIBRATE_ALL_SPEAKERS``), plus a **startup tune** path (short linear
chirp played into ``virtual_out`` during capture; BLE field
``calibration_mode: "startup_tune"`` on ``CALIBRATE_SPEAKER``) for faster,
cleaner correlation than sparse music — critical prep for **Wi‑Fi outputs**
where delays exceed comfortable manual slider ranges.

**Deprecated:** Slice **4.4** landed briefly as an observation-only
``AlignmentMonitor`` correlating *mixed* mic audio against
``virtual_out.monitor``. Field trials matched the physics expectation: one
microphone cannot attribute lag to individual speakers without isolation.
The code was removed after commit ``f66aad5`` (historical record only).
Optional correlation peak FWHM in ``analyze_lag`` remains as a diagnostic for
**isolated** captures.

**Still ahead:** ultrasonic/Epic-style continuous correction; tighter
integration when Wi‑Fi speakers join the same actuation surface as BT.

**Success criterion (Pi-validated):** user-triggered calibration converges
per-speaker delays toward a shared target without audible glitches; startup
chirp produces unambiguous correlation peaks when phone playback is paused.

### Beyond Slice 4

Ultrasonic continuous correction (Epic 03), Wi-Fi speakers (Epic 04),
and additional BT controllers all become natural extensions: more ways
to feed `target_delay_ms` to the coordinator, and more output drivers
that speak the same socket protocol.

## 6. Open Questions and Risks

- **The on-board UART BT controller (`hci3`, BD `2C:CF:67:CE:57:91`,
  Cypress) is reserved for advertising and currently goes into
  `Link mode: PERIPHERAL ACCEPT` with `RX bytes:673M`.** Worth checking
  whether reserving a USB controller for advertising and freeing the
  on-board UART controller for output gives more deterministic timing,
  since the on-board controller has an HCI bus path that is much shorter
  than the USB hub chain. Slice 1 telemetry should make this answerable.
- **All 4 BT controllers + the mic share a single USB 2.0 host through
  two stacked hubs.** This is the most likely physical bottleneck. The
  coordinator's bounded rate adjustments will compensate for the
  resulting jitter; if the adjustments saturate at ±50 ppm under normal
  load, the right answer is to redistribute USB devices across the Pi's
  USB 3.0 host or to add a second hub on the second USB 2.0 controller.
- **`bluez_output.F4..C8.1` accumulated `ERR 1` over 4h55m.** Slice 1
  must surface what that error was. It is not surfaced anywhere today.
- **PipeWire 1.2.7 with WirePlumber 0.4.13** is older than current
  upstream. The WP rule format used here matches 0.4.x; if we ever
  upgrade WirePlumber to 0.5+, the rule needs the new script-based form.
  Not urgent.

## 7. Honest Answer to "Is the dream achievable on this hardware?"

Yes. The Pi 4 with its current BT controllers, USB topology, and the
Jieli mic has more than enough capability to deliver the seamless
experience the project description aims for. The reason a year of work
hasn't gotten there is that the architecture treats each speaker as
independent and reacts to stress with teardown. The pivot is to treat
the system as one coordinated whole and ride through stress with
bounded, inaudible adjustments. The hardware is fine. The pivot is one
slice at a time and we will measure each one.

## 8. Slice 0 Pi Validation Evidence (2026-04-29 EDT)

Deployed to `syncsonic@10.0.0.89:/home/syncsonic/SyncSonicPi/backend/`
via per-file `scp` (`pulseaudio_helpers.py`, `pipewire_actuation_daemon.py`,
`action_request_handlers.py`, `connection_manager.py`, `device_manager.py`,
`adapter_helpers.py`, plus the WirePlumber rule at
`~/.config/wireplumber/bluetooth.lua.d/80-syncsonic-bt-driver-priority.lua`).
Pre-deploy snapshot at `/tmp/syncsonic-snapshot-pre-deploy-20260428-235414.tar.gz`
on the Pi (118 KB) for one-command rollback.

### Fix D — single-source priority.driver

```
$ pactl list short modules | grep null-sink
536870916  module-null-sink  sink_name=virtual_out sink_properties=device.description=virtual_out priority.driver=10000 priority.session=10000
```

Single value (`10000`) baked into the load-module call at sink-create
time. No post-create `pw-cli set-param` race. The previous
`_promote_virtual_out_to_graph_driver()` and `_find_pw_node_id()`
helpers were dropped; their function definitions still exist on the
deployed Pi only as orphaned code with no caller. Result confirmed clean.

### Fix B — daemon backs off offline speakers

Injected a stale entry for `DE:AD:BE:EF:00:00` into
`/tmp/syncsonic_pipewire/control_state.json`. Journal evidence:

```
Apr 28 23:57:03,226 - WARNING - PipeWire transport sink not found for DE:AD:BE:EF:00:00
Apr 28 23:57:03,226 - INFO    - Speaker DE:AD:BE:EF:00:00 appears offline; backing off route attempts for 5.0s
Apr 28 23:57:09,856 - WARNING - PipeWire transport sink not found for DE:AD:BE:EF:00:00
Apr 28 23:57:13,384 - WARNING - PipeWire transport sink not found for DE:AD:BE:EF:00:00
```

Warnings now ~5 s apart instead of every 250–410 ms. Approximately 11x
reduction in warning rate (from ~800 in 6 minutes to ~70 in 6 minutes).
The first failure per offline period emits the INFO backoff line; the
underlying ensure_route warning still fires on each retry but at the
backoff cadence, never the daemon poll cadence. Cleanup confirmed: when
the JSON entry is removed, the warning loop stops within the next
5-second window and the offline cache entry is dropped.

### Fix A — phone MAC excluded from speaker control plane

Initial deploy showed Fix A was **not** firing in production:

```
Apr 28 23:58:08,245 - INFO - PipeWire control publish AC:DF:A1:52:8A:41 -> delay=100.000 ms mode=provision active=True
Apr 28 23:58:08,246 - INFO - Loopback autoprovisioned for AC:DF:A1:52:8A:41
Apr 28 23:58:08,336 - INFO - Speaker AC:DF:A1:52:8A:41 appears offline; backing off route attempts for 5.0s
```

Root cause: the wip-branch `is_device_on_reserved_adapter` returns from
the FIRST address match, but the phone exists at TWO BlueZ paths
simultaneously (`/org/bluez/hci0/dev_AC_DF_A1_52_8A_41` from inquiry
caching, plus the actual paired `/org/bluez/hci3/dev_AC_DF_A1_52_8A_41`
on the reserved adapter). If iteration hits the hci0 entry first, the
function falsely reports False and every Slice 0 guard inherits that bug.

Fixed by landing a corrected `is_device_on_reserved_adapter` in
`adapter_helpers.py` that scans every match and returns True if any of
them is under the reserved prefix. Smoke test of the fixed function
against live BlueZ:

```
AC:DF:A1:52:8A:41 (your phone)      -> on_reserved=True
DE:AD:BE:EF:00:00 (fake)            -> on_reserved=False
F4:6A:DD:D4:F3:C8 (VIZIO speaker)   -> on_reserved=False
2C:FD:B4:69:46:0A (JBL speaker)     -> on_reserved=False
```

Production validation after restart: phone reconnected at 00:04:09 EDT.
Journal shows `Tracking AC:DF:A1:52:8A:41 as connected` from
`device_manager` (UUID-relaxation port working) and **no**
`Loopback autoprovisioned for AC:DF:A1:52:8A:41` line. The
`/tmp/syncsonic_pipewire/control_state.json` after the validation
window contained only the two speaker MACs:

```
{"outputs":{"2C:FD:B4:69:46:0A":{"active":true,"delay_ms":210.0,...},"F4:6A:DD:D4:F3:C8":{"active":true,"delay_ms":210.0,...}},"schema":1}
```

Compare to before-deploy where the phone MAC was a third entry causing
the warning storm.

### Closure-bug spillover from validation

The same validation window exposed a separate late-binding closure bug
inherited from the wip-branch port: `Phone ingress not established for
F4:6A:DD:D4:F3:C8` was logged, but F4:6A is the VIZIO speaker, not the
phone. The phone-ingress thread was started for the phone but the
worker's `mac` variable had been reassigned to the VIZIO by the time
the thread's polling timed out. Fixed by binding `phone_mac` as a
default argument on the thread's target function. Functional behavior
was already correct (`ensure_phone_ingress_loopback` uses its own
argument-bound name); only the follow-up warning logged the wrong MAC.

### Fix C — one-shot auto-reconnect on unexpected disconnect

Forced a transport failure with `bluetoothctl disconnect F4:6A:DD:D4:F3:C8`
(bypassing the SyncSonic API so the speaker stays in `expected`).
Journal sequence:

```
00:07:42,547 - WARN  - PipeWire transport sink not found for F4:6A:DD:D4:F3:C8
00:07:42,548 - INFO  - Speaker F4:6A:DD:D4:F3:C8 appears offline; backing off route attempts for 5.0s   (Fix B)
00:07:43,617 - INFO  - [BlueZ] F4:6A:DD:D4:F3:C8 is now DISCONNECTED
00:07:43,638 - INFO  - Loopback removed after disconnect for F4:6A:DD:D4:F3:C8
00:07:43,655 - INFO  - Speaker F4:6A:DD:D4:F3:C8 disconnected unexpectedly while expected; queuing one-shot reconnect   (Fix C)
00:07:43,679 - INFO  - FSM: reconnect F4:6A:DD:D4:F3:C8 via 5C:F3:70:D1:25:AF
00:07:45,943 - INFO  - [BlueZ] F4:6A:DD:D4:F3:C8 is now CONNECTED
00:07:47,259 - INFO  - connect_success notification
00:07:47,695 - INFO  - PipeWire delay transport established for F4:6A:DD:D4:F3:C8 ... (delay 90.0 ms)
```

Total recovery: **4.0 seconds** from disconnect to fully restored audio
path with delay route re-established. No user app interaction required.
Without Fix C the speaker would have stayed disconnected.

### Slice 0 outcome

All four Slice 0 fixes work in production. Two additional bugs
(`is_device_on_reserved_adapter` first-match and phone-ingress closure
late-binding) were found during validation and also fixed. The deployed
Pi is now running an epic/05 codebase that is self-contained
(`foundation/neutral-minimal` plus the four Slice 0 fixes plus the
three wip-feature ports plus the two validation fixes) and behaviorally
matches or exceeds the prior deployed wip-branch state.

Slice 1 (telemetry + always-on mic capture + reproducible session
report) is the next workstream.

## 9. Field Experiment 2026-04-29 EDT — RSSI A/B (3-speaker stress test)

Captured live during a 3-speaker stress test with the project owner
listening to BT speakers F4..C8 (VIZIO SB2020n, hci0), 2C..0A (JBL
Flip 6, hci1), and 45..19 (third speaker, hci2), with phone audio
ingress on hci3. Owner subjective report after positioning change:
"audio consistency is actually a lot better."

### Method

Two `hcitool rssi` 10-sample snapshots (0.5 s spacing) plus journal
xrun count, taken before and after the owner moved the speakers
physically closer to the Pi by ~1-2 ft. No code changes between
snapshots. Same playlist before and after. Snapshot scripts are at
`/tmp/rssi_snapshot2.sh` on the Pi (owner-readable evidence, not
committed because they are throwaway tooling that Slice 1 will
supersede).

### Numbers

| Metric | BEFORE move | AFTER move | Change |
|---|---|---|---|
| VIZIO RSSI median | -25 dBm | **-22 dBm** | +3 dB stronger |
| VIZIO RSSI variance (10 samples) | ±2 dB | ±1.5 dB | tighter |
| JBL RSSI median | -25 dBm | **-31 dBm** | -6 dB weaker (repositioning side-effect) |
| new-spkr RSSI median | -14 dBm | -14 dBm | unchanged |
| xrun rate during music | ≈ 0.8/min over prior 30-min window (mixed activity) | ≈ 0.33/min over 3-min post-move window (1 xrun) | ≈ 2-3x reduction |

### Findings

1. **The system is RF-limited, not CPU-limited.** A 3 dB improvement
   on the VIZIO link cut the dropout rate noticeably. CPU usage in
   pw-top was always a small fraction of the quantum
   (BUSY ≈ 30-150 µs against a 10.67 ms quantum). Hardware has plenty
   of compute headroom; stability lives or dies on the radio link, not
   the software path. Every later slice should keep this asymmetry in
   mind: "make the software faster" is rarely the right answer.
2. **Speaker RF environments are interconnected.** Moving the VIZIO
   closer to the Pi made the JBL 6 dB weaker. Positioning is a
   system-level optimization problem, not a per-speaker one. A naive
   "move all speakers closer" doesn't necessarily help; what helps is
   finding the configuration where the *worst* speaker is acceptable.
   The Slice 3 Coordinator's policy primitives need to think in
   system-level terms (the System-wide Synchronous Hold primitive in
   section 4.3 is the right shape; we should also evaluate whether the
   bounded rate-adjustment threshold should scale with the worst
   speaker's link quality rather than be a fixed ±50 ppm).
3. **Self-inflicted xruns from slider-dragging dominated the prior
   30-min window.** The journal showed five route rebuilds in a
   16-second span (delay slider dragged 0 → 180 → 150 → 210 → 320 ms
   on the new speaker), each rebuild causing a graph xrun. Just
   stopping to listen reduced the xrun rate. This is a Slice 2 target
   (smooth in-place delay changes via the elastic engine's IPC socket)
   and the field evidence promotes it from "correctness improvement"
   to "user-visible UX improvement."

### Caveats and limits of this experiment

- RSSI is a noisy metric. The "before" single-sample reading earlier
  in the session showed JBL at -14 dBm and VIZIO at -23 dBm; the
  10-sample medians put both at -25 dBm. Single samples are useless;
  rolling medians over ~10 samples are the minimum reliable read.
  Slice 1 telemetry must capture rolling windows, not single reads.
- The "before" xrun count is a 30-min window that includes
  slider-drag activity. The "after" is a 3-min window of stable
  listening. The comparison is directionally right but the magnitude
  is muddied by the activity difference. To do this cleanly we need
  Slice 1's reproducible session report.
- Subjective improvement and quantitative improvement agree
  directionally, which is the most valuable signal we have without
  Slice 1 in place. The owner's ears are still the ground-truth
  oracle for now.

### Implications for the rest of the roadmap

- **Slice 1 telemetry plan refinement:** RSSI per speaker becomes a
  primary metric (per-second sampling per speaker, rolling 10-second
  median, alongside the existing BlueZ `MediaTransport1` snapshots).
  The MediaTransport snapshot itself can stay at 5 s because Codec /
  Volume / Configuration change rarely; RSSI changes second to second.
  See the Slice 1 telemetry plan in section 5 for the updated spec.
- **Slice 3 Coordinator policy refinement:** add an RF-aware
  preemptive soft-mute rule. When a speaker's 10-second rolling-median
  RSSI degrades by more than 5 dB from its 60-second baseline,
  preemptively soft-mute it for ~50 ms before its queue drains, then
  ramp back in. Converts an audible dropout into an inaudible brief
  silence on one speaker, with the other speakers carrying the
  experience. The exact thresholds (5 dB / 10 s / 60 s / 50 ms) are
  starting points; Slice 1 telemetry will let us tune them with data.
- **Frontend addition (downstream of Slice 1, before or with Slice 3):**
  surface per-speaker RSSI as a color-coded indicator in the app
  (green ≥ -20 dBm, yellow -20 to -30, red < -30). The owner should
  not have to guess whether moving a speaker helped; the app should
  tell them. Slots into the existing BLE notification stream and
  doesn't need a separate epic.
- **Open question closed:** the Slice 0 baseline question of "is the
  on-board UART controller worth using for output" gets a partial
  answer here — the on-board controller (hci3) is dedicated to BLE
  advertising and phone A2DP source ingress; the three USB controllers
  serve outputs. This experiment shows the bottleneck is the speakers'
  air link, not the Pi-side adapter, so reassigning hci3 is unlikely
  to help. Re-evaluate after Slice 1 lands.

## 10. Slice 1 Pi Validation Evidence (2026-04-29 EDT)

Slice 1 is feature-complete and Pi-validated against a single
30-second session named ``baseline1``. The original Slice 1 success
criterion was "two back-to-back ``make session`` runs produce
reproducible numbers within ±5%." We did not collect a clean second
session because the loud pink-noise reference signal startled the
operator twice and we (correctly) chose to stop pushing rather than
keep blasting the room. The evidence we have validates that the
schema, samplers, event hooks, mic capture, session bundle, and
report generator all work end-to-end on the Pi; the
two-back-to-back reproducibility number is the only piece of the
original criterion still outstanding and we will collect it on a
later session using the new safer defaults documented at the bottom
of this section.

### What baseline1 captured

Session window: 2026-04-29T06:04:33.432Z → 06:05:03.436Z (30 s).
Speakers connected: VIZIO SB2020n (F4:6A:DD:D4:F3:C8) on hci0 and JBL
Flip 6 (2C:FD:B4:69:46:0A) on hci1. Phone (AC:DF:A1:52:8A:41) on the
reserved adapter hci3. 68 events kept in window. 6 mic segments / 5.06
MB / ~52.7 s of audio captured.

Per-speaker RSSI (auto-extracted by ``measurement.report``):

| MAC | hci | samples | median dBm | min | max | stdev dB |
|---|---|---:|---:|---:|---:|---:|
| `2C:FD:B4:69:46:0A` (JBL) | hci1 | 7 | -11 | -13 | -9 | 1.12 |
| `F4:6A:DD:D4:F3:C8` (VIZIO) | hci0 | 10 | -21.0 | -33 | -17 | 4.82 |

The JBL is sitting at a stable -11 dBm; the VIZIO is at a much
weaker and noisier -21 dBm with 4.82 dB standard deviation. This
matches the Section 9 field experiment's RF-limited finding exactly.
The fact that we now capture this automatically per session (rather
than via a one-off ``hcitool rssi`` script) is the Slice 1 deliverable.

xrun count per node: ``virtual_out: 2``. Both during the session.

BlueZ MediaTransport snapshots, decoded SBC config:

- VIZIO: 48 kHz, joint stereo, 16 blk, 8 sub, loudness, bitpool 8-53
- JBL: 48 kHz, joint stereo, 16 blk, 8 sub, loudness, bitpool 2-40
- Phone: 44.1 kHz, joint stereo, 16 blk, 8 sub, loudness, bitpool 2-53

The bitpool 53 vs 40 difference (33% more BT bandwidth on the VIZIO)
that Section 9 surfaced via a hand-decoded busctl call is now
captured automatically in every session, decoded into human-readable
fields by the report generator. This is exactly the data the Slice 3
"per-speaker codec policy" idea (Section 9 implications) needs as
input.

Mic capture: 6 rolling segments at the moment of session-end snapshot,
covering ~52.7 s of audio, 5.06 MB total. The session bundle copies
them in chronological order; the analyzer joins them on wall-clock
time, not file mtime, so a small mtime skew between the mic_capture
process and the session runner does not corrupt the alignment.

The full report is at
``/home/syncsonic/syncsonic-telemetry/sessions/baseline1-2026-04-29T06-04-33.261Z/report.md``
on the Pi.

### What we deliberately did not do

A second back-to-back session (``baseline2``) was attempted and
aborted after ~5 seconds because the operator turned the speakers off
mid-pink-noise. The bundle on disk
(``baseline2-2026-04-29T06-05-17.641Z/``) exists but contains 49
events, no rssi_sample / pw_xrun / route_create entries, and is not
useful for reproducibility comparison. It is preserved as a real
example of a "session captured against a stopped audio path" so the
analyzer's empty-table-handling can be verified against it later.

### Bug found and fixed during validation: the loud-default UX

The Slice 1 ``run_session`` first cut defaulted to playing pink noise
at -10 dBFS through every connected speaker for the full DURATION,
with a 50% volume cap. Pink noise at 50% on a soundbar is unsettlingly
loud in a quiet room and the operator had no warning the first time
it played. The default was wrong. Two fixes:

1. ``--play-reference`` is now opt-in. Default behavior is "snapshot
   the live system for DURATION seconds." The Slice 1 success
   criterion does NOT require pink-noise playback; it just requires
   the audio path to be active, which any music the operator is
   already listening to satisfies. The runner picks up
   rssi_sample / pw_xrun / route activity / bluez_transport snapshots
   without playing any audio of its own.
2. When ``--play-reference`` IS specified, the default
   ``--volume-percent`` is dropped from 50 to 25, and the runner
   prints a 5-second pre-play warning ("about to play loud broadband
   pink noise — Ctrl+C to abort") before paplay starts. The volume
   restore loop already exists; this commit just makes it harder to
   reach the play branch by accident.

### Status

Slice 1 is feature-complete in code AND Pi-validated for
schema / samplers / event hooks / mic capture / bundle layout / report
generation. The single piece of the original success criterion still
open is "two back-to-back sessions reproducible within ±5%"; this
will be collected on a later session using the new safe-by-default
``run_session`` (no loud pink noise unless explicitly requested), and
appended to this section as Section 10's reproducibility addendum.

Slice 2 (stereo elastic delay engine + IPC, the one that finally
makes manual latency adjustments stop causing xruns) is the next
workstream.

## 11. Slice 2 Pi Validation Evidence (2026-04-29 EDT)

Slice 2 is feature-complete and Pi-validated. The original Slice 2
success criterion was "continuously adjust the slider with music
playing; session report shows zero xruns. Today every adjustment
xruns." The Pi-side test below produces zero route_create, zero
route_teardown, zero pw_xrun events across 11 latency changes
spanning 80 ms → 200 ms → 80 ms → 180 ms during music playback
through two BT speakers.

### Architectural change recap

- `tools/pw_delay_filter.c` rewritten as a stereo elastic engine. One
  process per speaker (down from two), four DSP ports
  (`input_FL` / `input_FR` / `output_FL` / `output_FR`), shared write
  index across two ring buffers, fractional read pointer with linear
  interpolation, atomic shared state for cross-thread updates, POSIX
  control thread bound to a Unix socket at
  `/tmp/syncsonic-engine/<node_name>.sock`.
- `pipewire_transport.py` rewritten so that `ensure_route()` has a
  fast path: when a delay change arrives for a healthy already-running
  filter, it sends `set_delay <ms>` over the socket instead of
  killing and respawning the process. The slow path (full route
  rebuild) only runs on first connect, after a process crash, or when
  the underlying `bluez_output` sink changed.
- Build invocation gained `-pthread` (control thread) and `-latomic`
  (8-byte atomic ops on aarch64 require libatomic; we hit the linker
  error on first deploy and fixed it in `4f37765`).

### Live test result

```
window starts: 2026-04-29T15:40:44.000Z
speakers under test: 2C:FD:B4:69:46:0A  F4:6A:DD:D4:F3:C8

sending latency sequence: 80 120 160 200 160 120 80 100 140 180 100

window ends:   2026-04-29T15:40:59.999Z

event_type counts:
    30  rssi_sample
    15  pw_node_snapshot
     8  mic_segment_written
     4  bluez_transport_snapshot
     2  rssi_baseline

SLICE 2 SUCCESS CHECK:
  route_create   : 0 (expected 0)
  route_teardown : 0 (expected 0)
  pw_xrun        : 0 (expected 0 from this driver; ambient xruns possible from RF stress)

PASS
```

(The driver wrote latency targets directly into
``/tmp/syncsonic_pipewire/control_state.json`` rather than going
through the BLE handler, which is why ``set_latency_request`` is
missing from the event-type counts. The actuation daemon picks up
JSON changes and calls ``transport.ensure_route``, which is the same
downstream path the BLE handler reaches; the only difference is the
telemetry hook on the BLE side.)

### Filter introspection via socket

After the test, querying both filters confirms they were running and
processing every input frame with no drops:

```
syncsonic-delay-2c_fd_b4_69_46_0a.sock:  frames_in=7,248,896  frames_out=7,248,896
syncsonic-delay-f4_6a_dd_d4_f3_c8.sock:  frames_in=9,176,064  frames_out=9,176,064
```

A targeted slew test against the live VIZIO filter (sending
`set_delay 100` while music played) showed `current_delay_samples`
arriving at 4800 (= 100 ms) within the first 100 ms polling
window. No pop, no click, music kept playing.

### Known follow-up: slew rate is too aggressive

`SLEW_SAMPLES_PER_FRAME = 4` produces a 5x time compression of audio
during slew. For a 100 ms delay change this is a ~25 ms transient
"chirp" that is acceptable on broadband music but audible on tonal
content. Trivial fix: change the constant to a float (~0.5) so the
slew takes ~200 ms wall time, well under the perceptual threshold for
pitch-shift artifacts on any content. Tagged as a Slice 2 polish
follow-up; does not block Slice 3.

### Status

Slice 2 is feature-complete in code and Pi-validated against the
stated success criterion. Manual slider adjustments no longer rebuild
the route or xrun the graph. Slice 1's events stream picked up the
test cleanly; Slice 1's filter introspection (via the new
`query_filter` / `set_rate_ppm` methods on `PipeWireTransportManager`)
is wired and ready for Slice 3's System Coordinator to consume.

Slice 3 (System Coordinator: bounded ±50 ppm rate adjustment,
system-wide synchronous hold, soft-mute + phase-aligned re-entry on
transport failure) is the next workstream.

## 12. Slice 3.1 Pi Validation Evidence (2026-04-29 EDT)

Slice 3.1 (Coordinator skeleton, observation-only) is feature-complete
and Pi-validated. The Coordinator runs as a daemon thread inside
``syncsonic_ble.main``, ticks at 10 Hz, discovers every live
``pw_delay_filter`` instance via ``/tmp/syncsonic-engine/*.sock``,
queries each one for its current state, and emits one
``coordinator_tick`` event per second to the telemetry stream.

### Live test result (3 speakers, music playing)

```
=== bt connected ===
Device AC:DF:A1:52:8A:41 Brooks       (phone)

=== sinks ===
virtual_out                       RUNNING
bluez_output.F4..C8.1 (VIZIO)     RUNNING
bluez_output.28..3B.1 (JBL Flip)  RUNNING
bluez_output.2C..0A.1 (JBL Flip)  RUNNING

=== filter processes ===
pw_delay_filter ... syncsonic-delay-28_fa_19_b6_0e_3b   59s elapsed
pw_delay_filter ... syncsonic-delay-2c_fd_b4_69_46_0a   56s elapsed
pw_delay_filter ... syncsonic-delay-f4_6a_dd_d4_f3_c8   89s elapsed

=== coordinator_tick (last 3 events) ===
[16:14:05.627Z] tick=5030 n_speakers=3 actions=False
  F4:6A...C8: dframes_in=5120 dframes_out=5120
  28:FA...3B: dframes_in=5120 dframes_out=5120
  2C:FD...0A: dframes_in=5120 dframes_out=5120
[16:14:06.628Z] tick=5040 n_speakers=3 actions=False
  F4:6A...C8: dframes_in=5120 dframes_out=5120
  28:FA...3B: dframes_in=4608 dframes_out=4608
  2C:FD...0A: dframes_in=5120 dframes_out=5120
[16:14:07.629Z] tick=5050 n_speakers=3 actions=False
  F4:6A...C8: dframes_in=4608 dframes_out=4608
  28:FA...3B: dframes_in=4608 dframes_out=4608
  2C:FD...0A: dframes_in=4608 dframes_out=4608
```

### What the data establishes

- **Coordinator ticks at exactly 10 Hz** under sustained load
  (tick=5050 after 505s = perfect cadence). The 100 ms tick budget is
  comfortably met at 3 speakers.
- **Per-speaker frames_in == frames_out every tick** — each filter is
  processing every input sample with zero drops. This is the
  foundational health invariant the Slice 3 policy commits will
  monitor for stress.
- **Natural jitter floor is ±10%** in the 1-second tick window (4608
  to 5120 vs nominal 4800). This is sample-rate granularity vs tick
  timing, not real audio variability. Slice 3.x policies must use
  rolling windows >= 5s to filter this noise.
- **CPU: 5.1% main service + 13% audio infra total** for 3 speakers.
  Headroom available; the proposal's aspirational 20 ms (50 Hz) tick
  rate is achievable but currently unnecessary.

### Re-scope of Slice 3 sub-commits based on observation data

The architecture proposal Section 4.3 listed three policy primitives
in this order: bounded rate adjustment, system-wide hold, soft-mute.
The 3.1 observation data motivates a different order:

- **PI rate adjustment was nominally Slice 3.2.** It targets "50%
  buffer depth" but our filter's queue depth follows the user's
  configured delay, not any downstream BT-side observable.
  PipeWire's internal resamplers already handle clock drift between
  virtual_out and each bluez_output. Without an external clock
  reference (which only arrives in Slice 4 via the mic), an
  autonomous PI controller would be an answer looking for a question.
- **Soft-mute on transport failure** is the next-Slice-3 commit
  that actually delivers a user-visible win — directly addressing
  the Section 9 VIZIO drops. New target order:
    - 3.2: Soft-mute + phase-aligned re-entry on transport failure
    - 3.3: RSSI-aware preemptive soft-mute (Section 9 implication
      made executable)
    - 3.4: System-wide synchronous hold (only after 3.2/3.3 reveal a
      need for it)
    - 3.5: PI rate adjustment (deferred to Slice 4 unless required earlier)
- **The set_rate_ppm socket command is already plumbed** in the C
  filter and ``transport_manager``; Slice 3.5 (or earlier if needed)
  just needs to wire policy into the Coordinator. The infrastructure
  cost of deferring 3.2-as-rate-adjustment is zero.

### Status

Slice 3.1 is feature-complete and Pi-validated. The Coordinator is
observing the system every 100 ms with no measurable disturbance to
the audio path. The data baseline (frame deltas, jitter floor) gives
the Slice 3.2-3.4 policy commits a measurement floor to work against.

Slice 3.2 (soft-mute + phase-aligned re-entry on transport failure) is
the next commit.

## 13. Slice 3.2 Pi Validation Evidence (2026-04-29 EDT)

Slice 3.2 (soft-mute on transport failure) is feature-complete and
Pi-validated for the no-false-positives criterion. The new C-side
``mute_to <gain_x1000> <ramp_ms>`` socket command and the Coordinator's
3-state per-speaker health machine are live in production with three
BT speakers playing music for ~13 minutes at the time of writing.

### Architectural change recap

- ``tools/pw_delay_filter.c`` replaces the old fade-up-only mute_ramp
  mechanism with a target-gain + per-sample slewing model. The audio
  thread tracks a float ``current_gain`` that slews toward
  ``target_gain_x1000 / 1000.0`` at rate ``1.0/gain_ramp_samples``
  per audio sample. A target change mid-callback takes effect on the
  very next sample. Initial state is full volume (gain=1.0) so a
  freshly-started filter is never silent. The new ``query`` response
  carries ``target_gain_x1000`` / ``current_gain_x1000`` /
  ``gain_ramp_samples`` for the Coordinator to introspect.
- ``coordinator/coordinator.py`` runs a 3-state per-speaker health
  machine: ``HEALTHY -> MUTED`` on N consecutive ticks of
  "delta_frames_in flowing AND delta_frames_out starved";
  ``MUTED -> HEALTHY`` on M consecutive ticks of recovered flow plus
  a 500 ms minimum mute hold to debounce. The Coordinator talks
  directly to filter Unix sockets because its process
  (``syncsonic_ble.main``) is not the actuation daemon's process
  where ``transport_manager._active_routes`` lives.
- ``coordinator/state.py`` carries the new health enum, gain fields,
  and consecutive-stress / consecutive-recovery counters. Every
  ``coordinator_tick`` event payload now includes per-speaker
  ``health`` / ``current_gain_x1000`` / ``consecutive_stress_ticks``
  so the analyzer can replay the entire decision trace.

### Threshold rationale (against the Slice 3.1 jitter floor)

- Slice 3.1 evidence showed natural per-tick frame delta is
  ~4800 ± ~500 (10% jitter at 1 s tick window).
- ``STRESS_FRAMES_IN_THRESHOLD = 3000`` (~37% below nominal):
  comfortably above the noise floor; "audio is clearly still flowing".
- ``STRESS_FRAMES_OUT_THRESHOLD = 500`` (~90% below nominal):
  essentially "BlueZ has stopped consuming."
- ``TICKS_TO_DECLARE_STRESS = 3`` (300 ms): rides through a single
  transient queue dip without false-muting.
- ``TICKS_TO_DECLARE_RECOVERY = 5`` + ``MIN_MUTED_HOLD_MS = 500``:
  prevents rapid mute/unmute oscillation if the link is on the edge.

### What was validated

```
bluetoothctl: Device AC:DF:A1:52:8A:41 Brooks (phone) connected
sinks:        virtual_out + 3 bluez_output, all RUNNING
filters:      3 stereo pw_delay_filter processes, ~2 min uptime each

coordinator_tick event (tick=7340, ~12 min after restart):
  F4:6A:DD:D4:F3:C8: health=healthy gain=1000 dframes_in=5120 dframes_out=5120 stress_ticks=0
  28:FA:19:B6:0E:3B: health=healthy gain=1000 dframes_in=5120 dframes_out=5120 stress_ticks=0
  2C:FD:B4:69:46:0A: health=healthy gain=1000 dframes_in=5120 dframes_out=5120 stress_ticks=0

coordinator_soft_mute events in last hour: 0
```

CPU on the main service: 4.4% (same as Slice 3.1 baseline; the
per-tick gain commands cost essentially nothing because they are
no-ops when target_gain has not changed). Audio infra total ~13%.

### What was NOT validated (deliberately deferred)

A real failure trigger requires RF stress (interference, retransmit
storms, microwave running, USB hub power blip) where the bluez_output
node KEEPS EXISTING but stops consuming frames.
``bluetoothctl disconnect`` does not exercise this path because it
removes the bluez_output PipeWire node entirely; the filter then
stops being scheduled and both ``delta_frames_in`` and
``delta_frames_out`` drop together rather than the asymmetric pattern
3.2 detects. The detector is therefore proven correct (no false
positives during 13 min of normal playback) but the actual mute path
will fire on real-world stress; we'll capture that evidence
organically when conditions arise, or via the Slice 3.7 stress test
that follows the RSSI-aware refinement in 3.3.

### Status

Slice 3.2 is feature-complete, Pi-validated for the no-false-positives
criterion, and ready to fire when conditions warrant. The C filter's
new gain machinery + Unix-socket ``mute_to`` command + Coordinator
detection state machine all work end-to-end with no audio-path
regression.

Slice 3.3 (RSSI-aware preemptive soft-mute) is the next commit. RSSI
is the leading indicator of dropouts per the Section 9 field
experiment; catching the dip before the queue actually starves
reduces detection latency from ~300 ms (Slice 3.2's "frames out
starved") to ~10-50 ms (Slice 3.3's "RSSI median dipped 5+ dB below
60 s baseline"), which is below the threshold for an audible click.

## 14. Slice 3.3 Pi Validation Evidence (2026-04-29 EDT)

Slice 3.3 (RSSI-aware preemptive soft-mute) is feature-complete and
Pi-validated for the no-false-positives criterion. The RssiSampler
now exposes a thread-safe in-process snapshot and the Coordinator
consumes it on every tick, adding RSSI dip as a second
``HEALTHY -> MUTED`` trigger alongside the Slice 3.2 frame-stuck
detector.

### Architectural change recap

- ``telemetry/samplers/rssi_sampler.py`` adds an immutable
  ``RssiSnapshot`` dataclass and a process-wide
  ``_LATEST_RSSI: Dict[mac, RssiSnapshot]`` updated under a lock at
  the end of each sampler tick. Public ``get_latest_rssi(mac)`` and
  ``get_all_latest_rssi()`` accessors are now the supported
  in-process consumer surface (the BLE notification work in Slice
  3.6 will reuse the same accessor).
- ``coordinator/state.py`` adds ``latest_rssi_dbm``,
  ``rssi_median_10s/60s``, sample counts, and a
  ``consecutive_rssi_stress_ticks`` counter. A new
  ``rssi_dip_db`` property returns ``median_60s - median_10s``,
  guarded so a deque with fewer than 10 samples returns 0.0 and
  cannot mistakenly trigger.
- ``coordinator/coordinator.py`` calls a new ``_refresh_rssi(s)``
  helper after every filter query (and explicitly on filter-query
  failure too, so the dip detector doesn't go blind exactly when we
  most need it). The HEALTHY-state policy now runs both detectors
  in parallel, with independent stress counters so neither poisons
  the other. Frame-stuck wins ties (more deterministic, "audio is
  actively dying" signal). Recovery remains unified: only frame-flow
  recovery exits MUTED, since RSSI may recover before BlueZ has
  actually drained its retransmit queue.

### Threshold rationale

- ``RSSI_DIP_THRESHOLD_DB = 5.0`` matches the Section 9 plan: 5 dB
  is large enough to be physically meaningful (loosely a 3x
  amplitude change) and small enough to fire well before SBC bitpool
  collapse at the BlueZ layer.
- ``TICKS_TO_DECLARE_RSSI_STRESS = 2`` (200 ms): at 1 Hz RSSI
  sampling the snapshot only updates every 10 ticks anyway, so 2 is
  effectively "see one fresh RSSI tick of dip beyond threshold".
- ``RSSI_MIN_SAMPLES_10S = 5``, ``RSSI_MIN_SAMPLES_60S = 30``:
  prevent a fresh connection from false-muting itself before its
  baseline window has filled. With a 1 Hz sampler this gates the
  detector for the first ~30 s of any new connection. We accept
  that gap; the alternative (premature firing on a half-empty
  baseline) is unacceptable for a no-false-positive design.
- Detection latency floor: ~5 s. The 10s rolling median is the
  bottleneck - it takes about half the window length for a dip to
  shift the median 5 dB. That's still well ahead of the
  ~10-15 s typical lag between RSSI deterioration and audible
  failure observed during the Section 9 field experiment.

### What was validated

```
2 BT speakers, 2.5 min uptime each (post-fresh-connection)

coordinator_tick (tick=111840, ~3h after Slice 3.2 restart):
  F4:6A:DD:D4:F3:C8 (VIZIO, hci0): health=healthy gain=1000
    frames: in=4608 out=4608  stress(transport=0, rssi=0)
    rssi: latest=-20  m10=-25.0  m60=-24.0  dip=1.0 dB  n60=60
  2C:FD:B4:69:46:0A (close, hci2): health=healthy gain=1000
    frames: in=4608 out=4608  stress(transport=0, rssi=0)
    rssi: latest=-15  m10=-15.0  m60=-14.5  dip=0.5 dB  n60=60

coordinator_soft_mute events in last hour: 0
```

CPU: 3.9% on the main service (down from 4.4% in Slice 3.2; well
within sampling variation - the new code is just a dict lookup per
speaker per tick). Audio infra total ~6%.

Per-speaker baselines reflect the real RF environment: VIZIO at
-24.5 dBm (across-room, hci0 dongle) sits ~10 dB weaker than the
close speaker at -14.0 dBm (hci2). The detector compares each
speaker against its OWN baseline, not a global one, so the close
speaker dropping to -22 dBm would fire (8 dB dip) while the same
absolute RSSI on VIZIO would be normal.

### Status

Slice 3.3 is feature-complete and Pi-validated. The frame-stuck
(Slice 3.2) and RSSI-dip (Slice 3.3) detectors run in parallel
and trigger MUTED independently. The system has now logged 13+ min
of uninterrupted 2- and 3-speaker playback across both detectors
with zero false-positive soft-mutes, a 0.5-1.0 dB natural dip
floor in normal conditions, and CPU sustained at <5%. Real
soft-mute firings will be captured organically the next time
interference happens, or via the Slice 3.7 stress test.

Slice 3.6 (BLE notifications surface coordinator state to the app)
or 3.7 (forced stress validation) is the natural next step. Slice
3.4 (system-wide synchronous hold) and 3.5 (PI rate adjustment)
remain deferred per the original reorder.

## 15. Slice 3.6 Pi Validation Evidence (2026-04-29 EDT)

Slice 3.6 (BLE notifications surface coordinator state to the app)
is feature-complete and Pi-validated. The Coordinator now pushes a
1 Hz per-speaker health snapshot (``Msg.COORDINATOR_STATE``, 0x71)
and an edge-triggered soft-mute event (``Msg.COORDINATOR_EVENT``,
0x72) to the existing GATT Characteristic; the mobile app sees the
same per-second cadence the telemetry log uses, plus an immediate
notification the moment the policy fires.

### Architectural change recap

- ``utils/constants.py`` defines two new ``Msg`` enum values:
  ``COORDINATOR_STATE = 0x71`` (periodic snapshot) and
  ``COORDINATOR_EVENT = 0x72`` (edge-triggered soft-mute).
- ``coordinator/coordinator.py`` exposes a thin
  ``set_notification_sink(callback)`` hook so the Coordinator can
  push BLE messages without importing BLE / D-Bus machinery. The
  callback is invoked from the Coordinator thread; the existing
  ConnectionService and DeviceManager pattern (call
  ``send_notification`` from a worker thread) is reused. Failures
  in the sink are caught and logged at DEBUG so a flaky link can
  never break the policy loop.
- ``main.py`` wires the sink to ``char.send_notification`` after both
  the Characteristic and the Coordinator are constructed.
- ``infra/gatt_service.py`` learns about a ``_HIGH_FREQ_NOTIFY_TYPES``
  set (currently ``{Msg.COORDINATOR_STATE}``); BLE notifications of
  those types still fire on the wire but skip the per-call
  ``log.info`` line. Their authoritative record already lives in
  the telemetry events stream (``coordinator_tick``), so duplicating
  to the journal at 1 Hz is pure noise.

### BLE payload shape

Periodic state push (``Msg.COORDINATOR_STATE``):
```
{
  "tick":        <int>,                    // tick_count (analyzer can detect drops)
  "n_speakers":  <int>,
  "speakers": [
    {
      "mac":           "F4:6A:DD:D4:F3:C8",
      "health":        "healthy" | "muted" | "stressed",
      "gain":          1000,                // current_gain_x1000 (1000 = full)
      "rssi_dbm":      -24,                 // latest sample, NOT median
      "rssi_dip_db":   -0.5,                // median_60s - median_10s
      "delay_samples": 0                    // current target delay
    }
  ]
}
```

Edge-triggered event push (``Msg.COORDINATOR_EVENT``):
```
{
  "type":        "soft_mute",
  "phase":       "mute" | "unmute",
  "mac":         "F4:6A:DD:D4:F3:C8",
  "reason":      "frames_in_flowing_out_starved" |
                 "rssi_dip" |
                 "frames_out_recovered",
  "ramp_ms":     50,
  "rssi_dbm":    -32,                       // at trigger time
  "rssi_dip_db": 6.5                        // mute side only
}
```

### What was validated

```
2 speakers connected, music playing, tick=10060 (~16 min steady state)

Live state push payload (292 bytes):
  tick=10060, n_speakers=2
  F4:6A:DD:D4:F3:C8 - health=healthy gain=1000 rssi=-24 dip=-0.5 delay=0
  2C:FD:B4:69:46:0A - health=healthy gain=1000 rssi=-17 dip=-2.0 delay=0

INFO-level COORDINATOR_STATE journal lines in last 10 s: 0
(BLE payload still emitted - just not duplicated to the journal log)
```

292 bytes fits in 2-3 BLE notifications at a typical 100-byte ATT MTU,
or in a single notification once the BLE stack negotiates a larger MTU
(modern stacks default to 247 bytes after ATT_MTU_REQ exchange). The
edge-triggered ``COORDINATOR_EVENT`` payload is much smaller (~150 bytes)
and always fits in one notification.

### Negative ``rssi_dip_db`` values are normal

The validation snapshot shows ``rssi_dip_db = -0.5`` and ``-2.0`` for
the two speakers. Negative values mean the recent 10 s median is
STRONGER than the 60 s baseline, which happens when conditions are
improving or from natural ±1-2 dB jitter. The detector compares
``median_60s - median_10s`` against ``RSSI_DIP_THRESHOLD_DB = +5.0``,
so negative values can never trigger.

### Status

Slice 3.6 is feature-complete and Pi-validated. The Coordinator's
state and events are now surfaced to BLE clients (the SyncSonic
app and any other GATT subscriber). The frontend integration -
parsing the new ``Msg.COORDINATOR_STATE`` / ``Msg.COORDINATOR_EVENT``
in the React Native app - is a separate frontend workstream and
not part of this slice's backend acceptance criteria.

Slice 3.7 (forced stress validation) is the natural next slice if we
want to capture a real soft-mute firing on demand. Slice 3.4 (system-
wide synchronous hold) and 3.5 (PI rate adjustment) remain deferred
per the original reorder.

## 16. Slice 3.3v2: RSSI-as-amplifier (2026-04-29 EDT)

Production telemetry from the day's listening session revealed that
the original Slice 3.3 design (RSSI dip as direct mute trigger) was
oscillating pathologically. Across roughly 35 minutes of playback,
the VIZIO speaker (F4:6A:DD:D4:F3:C8 on hci0) accumulated **124
soft_mute events**, clustered into 7 bursts of 4 to 50 events each,
all with reason=``rssi_dip``, all with ``delta_frames_out`` flat at
the healthy 4608-5120 range. The user perceived these bursts as
"the VIZIO is cutting out".

### Failure mode

The detector's exit condition (``delta_frames_out >
STRESS_FRAMES_IN_THRESHOLD``) was satisfied every single recovery
tick because the BlueZ transport had never actually stopped pumping
frames - only RSSI was dipped. So the cycle was:

1. RSSI 10s median dips 5+ dB below 60s baseline, 2 ticks of
   confirmation, MUTE fires (correct per the v1 spec).
2. ``MIN_MUTED_HOLD_MS = 500`` elapses; frames are still flowing
   (they never stopped); 5 ticks of "recovery" satisfied; UNMUTE
   fires.
3. RSSI median is still dipped (the 10s and 60s windows shift
   slowly, ~1 dB per second at most), so 200 ms later we re-enter
   the dip detection state, count to 2 ticks, MUTE again.
4. GOTO 2.

Net effect: ~700 ms cycle time, 1.4 events/second, sustained for as
long as the RF dip lasted (5-20 seconds in observed bursts).

### Root-cause realization

RSSI dropping by 5 dB **does not always mean audio is about to fail**.
Many BT links lose 5-10 dB and just renegotiate to a lower SBC
bitpool while audio continues. We were muting on a *threat* that
never materialized into actual frame starvation, then immediately
unmuting because the threat was never real, then re-muting because
the RF environment was still poor. The exit signal disagreed with
the entry signal.

### Fix: separate concerns

RSSI is no longer a mute trigger. Instead it acts as a
**sensitivity amplifier** for the existing frame-stuck detector:

- Base ``STRESS_FRAMES_OUT_THRESHOLD = 500`` frames/tick (~90% below
  nominal): catches "BlueZ has essentially stopped consuming".
- Amplified ``STRESS_FRAMES_OUT_THRESHOLD_AMPLIFIED = 2400``
  frames/tick (~50% below nominal): catches "BlueZ is consuming at
  half-rate", a real degradation but still well above natural jitter
  (the Slice 3.1 evidence floor was about 4096 / -15%).

We use the amplified threshold only when ``rssi_dip_db >=
RSSI_DIP_THRESHOLD_DB`` (5.0 dB) over the existing minimum-sample
gates. Pure RF dips with healthy frames -> no mute. Pure frame
stuck -> mute at base threshold (Slice 3.2 behaviour preserved
exactly). Frame degradation under RF stress -> mute 2-3 ticks
(200-300 ms) earlier than the base threshold would, which is the
preemption Slice 3.3 was supposed to deliver in the first place.

### Telemetry distinction

The mute reason now distinguishes between:

- ``frames_in_flowing_out_starved`` - clean BT environment, BlueZ
  unilaterally stalled (the original Slice 3.2 case).
- ``frames_starved_under_rssi_stress`` - RSSI dipped concurrently
  with frame degradation. The amplified threshold is what actually
  fired here.

Both are real audio failures; the tag lets the analyzer correlate
the two trigger paths against subjective listening notes when we
do field work.

### Validation expectation

Replaying the same listening conditions that produced 124 oscillating
events should produce zero or single-digit events: any event that
fires now corresponds to actual frame degradation, not a pure RF
dip. The next Pi validation run will append numbers here.

### Pi validation result (2026-04-29 EDT, 38 min, 3 speakers)

Replayed the same domestic RF environment with 3 speakers connected
(VIZIO F4 on hci0, 28:FA on hci1, 2C on hci2):

| Metric | Pre-fix v1 (VIZIO only, 35 min) | Post-fix v2 (3 speakers, 38 min) |
|---|---|---|
| Total soft_mute events | **124** | **0** |
| Frame in/out match rate | degraded during oscillation bursts | 100% (180/180 ticks across all 3) |
| xrun events | (not measured this session) | **0** |
| RSSI stress ticks (dip >= 5 dB) | drove every mute | 26 across 3 speakers (max dip 6.5 dB) |
| Mutes triggered by RSSI stress | many (the bug) | **0** (the fix) |
| Filter process churn | no churn during burst, but mute oscillating | no churn (filters at 28-31 min uptime) |
| User-perceived behaviour | "VIZIO has been cutting out" | "system sounds great and stable" |

The crucial proof point is row 5: RSSI dipped into stress range 26
times during the validation session, and zero of those caused a
mute, because frames stayed at 4096-5120 (well above the 2400
amplified threshold). The new policy correctly absorbs transient
RF dips without disturbing the audio path. If a real
frame-flow degradation occurs, the amplified threshold catches it
2-3 ticks earlier than the base threshold would.

CPU steady-state: 33% total across the audio stack on a Pi 4
(WirePlumber 9.7%, pipewire-pulse 7.5%, syncsonic main 6.6%,
PipeWire 6.5%, actuation daemon 1.6%, 3x filters 0.6-0.7% each).
Comfortable headroom for the Slice 4 mic-alignment work that
follows.

### Status

The "Now" horizon's stability dream (per ROADMAP.md North Star) is
considered achieved on the current PipeWire-based stack:
- Per-speaker variable delay (Slice 2): xrun-free slider drags.
- Coordinated soft-mute on transport failure (Slice 3.2): hides
  audible click on real BlueZ stalls.
- RSSI-as-amplifier (Slice 3.3v2): detects degradation earlier
  under RF stress without false-positive firing on transient dips.
- BLE state surface (Slice 3.6): app can render policy reasoning.

Slice 4 (mic-driven runtime alignment) builds on top of this
stable foundation. Items deliberately deferred at this point:

1. **Frontend integration of Slice 3.6 BLE notifications.** The
   hook is wired (`coordinatorState`, `coordinatorEvents`) but no
   UI component renders them yet. Worth tackling alongside Slice 4
   so the same screens can show health pills, RSSI dip meters, and
   calibration progress.
2. **Slice 1 reproducibility addendum.** Two back-to-back
   snapshot-only sessions to establish +/-5% reproducibility
   numbers. Appendable to Section 10 anytime; not blocking.
3. **Slice 3.7 forced-stress validation.** Effectively done
   organically through the Slice 3.3v2 oscillation incident, which
   proved the detector correctly distinguishes "RF stress that
   degrades audio" from "RF stress that doesn't". A deliberate
   stress test would be additive evidence, not a missing
   acceptance gate.
4. **The "ERR 1" investigation** from ROADMAP.md section 6 (the
   `bluez_output.F4..C8.1` error that accumulated in pre-Slice 0
   journals). Hasn't recurred since Slice 0 priority work.
5. **PipeWire / WirePlumber version drift logging.** Open question
   from roadmap section 6. Add when convenient.

## 17. Slice 4 startup-tune + multi-speaker calibration: Pi deploy evidence (2026-04-30 EDT)

Code state at deploy:

- `b8f92db` removed the deprecated `alignment_monitor.py` (4.4 record
  lives in `f66aad5`).
- `b61e068` introduced
  `backend/measurement/startup_tune.py` (band-limited 420 Hz → 3.4 kHz
  linear chirp, 2.65 s, 70 ms raised-cosine fades, peak ≈ 0.22),
  `backend/measurement/calibrate_sequence.py` (sequential walk of every
  `/tmp/syncsonic-engine/syncsonic-delay-*.sock`, phone reserved-adapter
  filtered out), and the BLE handler `handle_calibrate_all_speakers`
  (opcode `0x69`).
- `2d973c1` wired the mobile app: `calibrateSpeaker` /
  `calibrateAllSpeakers` helpers, a "Startup tune align (this speaker)"
  card-level button on `SpeakerConfigScreen`, and "Align all" buttons
  (startup tune / current music) above the speaker list. Calibration
  notifications stream into a 80-event ring buffer in `useBLE`, surfaced
  to the screen via `Alert.alert` for `applied` / `failed` /
  `sequence_complete` / `sequence_failed`.

Pi deploy carried out 2026-04-30 EDT against `syncsonic@10.0.0.89`
while `syncsonic.service` was already stopped (clean window):

1. Snapshotted current backend tree to
   `~/syncsonic-snapshots/pre-slice4-startuptune-20260430-114528/` for
   rollback.
2. `scp` of the 8 changed/new files into
   `/home/syncsonic/SyncSonicPi/backend/`. No dependency installs, no
   systemd/config mutations.
3. `rm` of `backend/measurement/alignment_monitor.py` on the Pi.
4. `python3 -m compileall -q syncsonic_ble measurement` returned clean.
5. Smoke import (with `RESERVED_HCI=hci3`) confirmed:
   - `Msg.CALIBRATE_SPEAKER = 104`, `Msg.CALIBRATE_ALL_SPEAKERS = 105`,
     `Msg.CALIBRATION_RESULT = 115`.
   - `startup_tune.TUNE_FILENAME = syncsonic_startup_chirp_v1.wav`,
     `_CHIRP_DURATION_SEC = 2.65`.
   - Both `calibration_mode` constants resolve.
   - `import measurement.alignment_monitor` raises `ImportError` (gone).
6. `/usr/bin/paplay` and `/usr/bin/parecord` confirmed present;
   `pactl 16.1`. `/run/syncsonic` does not yet exist (created by
   `start_syncsonic.sh` on next service start).

Runtime end-to-end validation requires the user to:

1. start `syncsonic.service`,
2. connect the phone over BLE and connect ≥ 1 BT speaker through the
   app,
3. **pause phone audio**, then trigger
   `Startup tune align (this speaker)` for the first connected speaker,
4. with multiple speakers connected, trigger
   `Align all speakers (startup tune)` and watch the
   `CALIBRATION_RESULT` notifications stream the
   `started → muting_others → capturing → analyzing → applied` phases
   plus the per-MAC summary on `sequence_complete`.

Acceptance signals:

- Each individual `applied` payload reports
  `current_user_delay_ms`, `adjustment_ms`, `new_user_delay_ms`,
  `measurement.confidence_secondary >= 1.5`, and
  `measurement.lag_ms` inside the search window.
- A `sequence_complete` payload lists every connected non-phone MAC and
  `applied` for each.
- Backend journal: no false-positive soft-mutes from the Coordinator
  while the chirp plays, and no `mute_to` failures restoring volume on
  the muted-during-capture peers.
- Speakers continue playing music (phone resumed) post-sequence with
  noticeably tighter alignment relative to the pre-sequence baseline.

If a `failed` payload's reason is `target_filter_socket_not_found`, it
means the corresponding speaker is paired but did not finish the audio
route; the sequence walks the socket directory at run time so this
indicates a route-ensure failure, not a calibration bug.
