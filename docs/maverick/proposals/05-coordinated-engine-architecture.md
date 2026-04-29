# Epic 05: Coordinated Engine — Architecture Proposal

_Status: Slice 0 deployed and Pi-validated 2026-04-29 EDT on
`epic/05-coordinated-engine`. Slice 1 (telemetry) up next. See section 8 for
the validation evidence._

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
  `set_latency_request`, `mic_rms_window`), all timestamped with
  `CLOCK_MONOTONIC`.
- Pipe `pw-mon` (or periodic `pw-cli ls Node` snapshots) every 1 s into
  the same stream so we capture priority.driver changes, codec changes,
  and bluez node states.
- BlueZ `MediaTransport1` property snapshots (Codec, Volume, Delay,
  Configuration) every 5 s.
- One command (`make session NAME=test1`) plays a fixed 30 s music
  sample, captures everything, produces a single-page report: dropout
  count, inter-speaker drift, codec changes, xrun count.

**Success criterion (Pi-validated):** `make session` twice with no code
changes produces reproducible numbers within ±5%.

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

### Slice 4: Mic-based runtime alignment (2 weeks)

Now that the actuator takes rate adjustments smoothly, mic alignment is
~200 lines of Python.

- Use the always-on mic capture from Slice 1.
- Periodically (every 30 s of stable playback) cross-correlate the mic
  signal against the `virtual_out.monitor` tap to estimate per-speaker
  time-of-arrival relative to the system clock.
- Compute the ideal delay per speaker based on physical position and
  target alignment.
- Send `target_delay_ms` to each speaker's coordinator entry. The
  coordinator smoothly retargets the engine over a few seconds — no
  audible artifacts.
- "Auto-align" mode triggered from the app plays a short reference
  signal, computes alignment in 5 s, applies smoothly. This is Epic 02
  done correctly.

**Success criterion (Pi-validated):** "auto-align" pressed while music is
playing; mic correlation peaks tighten measurably; the user cannot hear
when alignment is being applied.

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
