# Epic 04: Wi‑Fi Speakers — Architecture Proposal

_Status: scoping draft, 2026-04-30 EDT, branch
`epic/04-wifi-speakers-manual-alignment`. This document is the
engineering plan; [`../epics/04-wifi-speakers-manual-alignment.md`](../epics/04-wifi-speakers-manual-alignment.md)
is the contract; [`../ROADMAP.md`](../ROADMAP.md) is the strategic frame._

This proposal is the rationale, slice plan, and risk register for
bringing Sonos / Wi‑Fi outputs back as **first‑class peers of the
Slice 2 elastic engine and Slice 4 mic-driven calibration loop**, not
as a parallel track.

## 1. Goal

Make a mixed Bluetooth + Wi‑Fi (Sonos) speaker set feel like a single
coherent audio environment — same app flow, same telemetry, same
calibration buttons. The user should be able to:

1. discover Wi‑Fi speakers from the app,
2. connect a Wi‑Fi speaker into the active configuration,
3. play audio through it in fan-out with the BT speakers,
4. press **Align all speakers (startup tune)** and have the Wi‑Fi
   delay measured and corrected automatically alongside Bluetooth,
5. fall back to a manual latency slider per Wi‑Fi output when an
   acoustic measurement is impossible.

## 2. Why This Lane Exists Now

Wi‑Fi speakers historically connected and played, but their large,
device-specific latency (typically **300–1500 ms** depending on Sonos
group state, codec, and Wi‑Fi conditions) lives well past the manual
slider's 0–500 ms range. Manual alignment was therefore guess-and-
listen and frequently impossible. Three things changed:

1. **Variable delay is now cheap.** The Slice 2 engine accepts up to
   `MAX_USER_DELAY_MS = 4000` per output without graph xruns
   ([`backend/measurement/calibrate_one.py`](../../backend/measurement/calibrate_one.py)).
2. **Mic measurement attributes lag per output.** Slice 4.2's
   sequential mute + cross-correlate captures one output at a time
   regardless of medium, so a Wi‑Fi output behaves identically to a
   Bluetooth one from the analyzer's point of view.
3. **The startup chirp is built for delays the slider can't reach.**
   The 2.65 s linear chirp gives a sharp correlation peak even when the
   round trip is several hundred ms; it is the right stimulus for
   Wi‑Fi.

The piece missing today is a **Sonos engine output**: a peer of
`pw_delay_filter`-fed `bluez_output.<MAC>` that the calibration
sequence enumerates by socket, the Coordinator observes, and the
mobile app drives through the same BLE control surface.

## 3. Where We Stand Today

Static inspection of `epic/05-coordinated-engine`:

- [`backend/syncsonic_ble/helpers/device_type_helpers.py`](../../backend/syncsonic_ble/helpers/device_type_helpers.py)
  still recognises `sonos:RINCON_...` device IDs.
- BLE opcodes `WIFI_SCAN_START / STOP / RESULTS` (`0x44 / 0x45 / 0x46`)
  are reserved in [`utils/constants.py`](../../backend/syncsonic_ble/utils/constants.py).
- All Wi‑Fi-specific handlers in
  [`state_change/action_request_handlers.py`](../../backend/syncsonic_ble/state_change/action_request_handlers.py)
  early-return `feature_disabled` (`wifi_speakers`).
- Frontend has full UI scaffolding: `DeviceSelectionScreen` accepts
  `deviceType=wifi`, `useBLE` keeps `wifiScannedDevices` state, and
  there is a Wi‑Fi entry point in `app/settings/config.tsx`.
- The historical README mentions Icecast + FFmpeg for the Sonos audio
  path. Source code for that path is not on the neutral foundation; the
  experimental branches (`wip/01-pipewire-transport-phone-ingress` and
  earlier) carry it but are explicit research-only.

So the lane is gated mostly by **backend logic**: discovery,
output-side audio plumbing, and engine-output integration.

## 4. Architecture: Wi‑Fi as a Peer Engine Output

### 4.1 The contract

A "Wi‑Fi engine output" looks identical to a Bluetooth one from the
Coordinator and calibration's perspective:

- a `pw_delay_filter` instance owning two stereo ports
  (`output_FL` / `output_FR`),
- a Unix socket at
  `/tmp/syncsonic-engine/syncsonic-delay-<token>.sock` (token is a
  filesystem-safe form of the Sonos device ID, e.g.
  `sonos_rincon_xxxx`),
- the same `set_delay`, `mute_to`, `query` wire format,
- whatever the filter writes downstream is irrelevant to the engine —
  for BT it goes into `bluez_output.<MAC>`; for Wi‑Fi it goes into a
  network sink.

The change is **purely on the output side of the filter**.

### 4.2 The audio path

```
phone ─ A2DP ──► virtual_out (null sink)
                    │
                    ▼ monitor
              ┌─────────────────────┐
              │  pw_delay_filter    │  per BT speaker (existing)
              │  delay/mute/gain    │
              └────────┬────────────┘
                       └──► bluez_output.<MAC>

              ┌─────────────────────┐
              │  pw_delay_filter    │  per Wi‑Fi speaker (NEW)
              │  delay/mute/gain    │
              └────────┬────────────┘
                       └──► sonos_pipe_<token>  (module-pipe-sink)
                              │
                              ▼
                       ffmpeg (encode AAC/MP3 + meta)
                              │
                              ▼
                       icecast2  ⇆  Sonos plays from URL
```

We deliberately keep the well-tested Icecast/FFmpeg encoder pipeline
because:

- Sonos players consume HTTP audio reliably (they advertise as
  Internet Radio sources);
- Icecast handles re-connect, codec metadata, and listener counts for
  free;
- the Pi already has both packages by README.

### 4.3 Discovery and association

Two reasonable paths:

- **SoCo (recommended).** A pure-Python library wraps Sonos UPnP/SOAP.
  Lets the backend list devices, group state, and trigger a "play this
  URL" command when the user connects a Sonos. No new daemon required.
- **Bare HTTP.** Direct UPnP discovery (SSDP) plus hand-rolled SOAP.
  Works without a new dependency but is more code to maintain.

We start with SoCo behind `requirements.txt`; falling back to bare
HTTP is a contingency, not a default.

### 4.4 Telemetry / Coordinator integration

`pw_delay_filter` already emits queue depth and frame counters over
its query socket. The Coordinator's `discover_speakers` already walks
`SOCKET_DIR`; with no Coordinator-side change, a Wi‑Fi engine output
shows up automatically. The only **policy** difference is that
`HEALTHY → MUTED` transitions for a Wi‑Fi output will not correspond to
"BlueZ transport stalled" — they will correspond to "Icecast source
stalled" or "Sonos closed the listener". We accept that the existing
frame-stuck detector covers both cases without modification.

RSSI does not exist for Wi‑Fi outputs. The Coordinator's
`rssi_dip_db` simply stays `0.0` for those entries; the
RSSI-as-amplifier path (Slice 3.3v2) silently no-ops on them. No
Coordinator code change required for v1.

## 5. Slice Plan

| Slice | Outcome | Effort |
|---|---|---|
| W1 | Discovery + presence (read-only) — `WIFI_SCAN_START/RESULTS` returns real Sonos topology; UI populates without playback | 2–3 days |
| W2 | Engine output adapter — pipe-sink + FFmpeg + Icecast plumbing wrapped in `wifi_transport.py` so a connected Sonos appears in `/tmp/syncsonic-engine/` and is observed by the Coordinator | 1 week |
| W3 | Calibration integration — `CALIBRATE_SPEAKER` + `CALIBRATE_ALL_SPEAKERS` with `startup_tune` measure Wi‑Fi lag accurately; `MAX_PLAUSIBLE_LAG_MS` raised to 1500 only inside the Wi‑Fi path; manual slider's display range extended for Wi‑Fi outputs | 3–4 days |
| W4 | Manual fallback + UI parity — slider per Wi‑Fi output up to 4000 ms (matches `MAX_USER_DELAY_MS`); explicit "Pause phone audio first" prompt before startup tune; status pills reuse `coordinatorState` | 2–3 days |

Each slice is independently deployable and produces objective evidence
on the Slice 1 telemetry stream.

### 5.1 Slice W1 — Discovery and presence (read-only)

**In scope**

- New module `backend/syncsonic_ble/wifi/sonos_discovery.py` that uses
  `soco.discover()` (or `soco.discovery.scan_network`) on a worker
  thread.
- `WIFI_SCAN_START` triggers a one-shot scan; results emitted via
  `WIFI_SCAN_RESULTS` notifications in the existing payload shape:
  `{ wifi_devices: [{ device_id: "sonos:RINCON_...", name, ip, type }] }`.
- A `Msg.CONNECT_ONE` handler accepts a `sonos:` ID and writes a stub
  configuration record without yet wiring audio. Returns
  `feature_partial` instead of `feature_disabled`.
- Frontend renders the scan list (already wired) and shows a "Wi‑Fi
  output, audio path coming next slice" status badge.

**Validation**

- App can scan and see Sonos devices.
- Connecting a Sonos shows it in the configuration UI but does **not**
  play.
- No regressions on Bluetooth flows (re-run Slice 4.2 single-speaker
  calibration on a BT speaker, expect identical results).

### 5.2 Slice W2 — Engine output adapter

**In scope**

- `backend/syncsonic_ble/helpers/wifi_transport.py` mirroring the
  shape of `pipewire_transport.py`:
  - `ensure_wifi_route(device_id, latency_ms)` creates
    `pw_delay_filter` instance + downstream `module-pipe-sink` named
    `sonos_pipe_<token>` + spawns `ffmpeg` reading from the pipe and
    pushing to local Icecast at `http://127.0.0.1:8000/syncsonic.aac`,
  - `start_sonos_playback(device_id, stream_url)` issues `Play(URI)` to
    the Sonos via SoCo,
  - `remove_wifi_route` tears the chain down cleanly.
- The filter's socket name reuses the `syncsonic-delay-<token>.sock`
  convention so the calibration sequence walks Wi‑Fi outputs without
  changes.
- `start_syncsonic.sh` learns to start `icecast2` on demand if the
  configuration includes any Wi‑Fi outputs.

**Validation**

- One Sonos plays the same audio currently routed to BT speakers.
- `pactl list short sinks` shows the new pipe sink.
- The Coordinator emits `coordinator_tick` payloads that include the
  Wi‑Fi entry; `frames_in == frames_out` on healthy ticks.
- No xruns introduced on the BT path — apparent from
  `pw_xrun` events in `events.jsonl` over a 30 s session.

### 5.3 Slice W3 — Calibration integration

**In scope**

- `MAX_PLAUSIBLE_LAG_MS` becomes a per-output policy: `700` for BT
  outputs (current), `1500` for Wi‑Fi outputs.
- `MAX_ADJUSTMENT_MS` similarly grows for Wi‑Fi (the first calibration
  cycle on a fresh Sonos may legitimately need to push ~1 s of delay
  shift).
- The startup chirp duration extends only when there is a Wi‑Fi output
  in the sequence (`max(2.65 s, max(measured Wi‑Fi window) + 0.6 s)`)
  so the analyzer always has both reference and capture covering the
  full round trip.
- `calibrate_sequence` emits a new diagnostic field `medium`
  (`"bluetooth" | "wifi"`) on every event.

**Validation**

- A two-speaker config (1 BT + 1 Wi‑Fi) converges both delays via one
  press of **Align all speakers (startup tune)**.
- `sequence_complete.per_mac_outcome` is `applied` for both.
- Listening test: playing music post-calibration, the BT and Wi‑Fi
  speakers are inside the perceptual threshold (~10 ms).

### 5.4 Slice W4 — Manual fallback + UI parity

**In scope**

- Slider on `SpeakerConfigScreen` clamps to `[0, 500]` for BT outputs
  but `[0, 4000]` for Wi‑Fi outputs (display detected from device ID).
- A persistent banner above any newly-added Wi‑Fi output that says
  "Pause phone audio before pressing **Startup tune align** for the
  cleanest measurement."
- Health pills below the speaker name driven by `coordinatorState` —
  `healthy` (green) / `muted` (yellow) / `stressed` (orange).
- README + AGENTS.md notes for Sonos + Icecast + FFmpeg setup.

**Validation**

- Manual alignment alone aligns a Wi‑Fi speaker within ~30 ms by ear
  even without the chirp.
- App + backend stay green on `npm run lint`, `npx tsc --noEmit`,
  `python -m compileall syncsonic_ble measurement`.

## 6. Open Questions and Risks

- **Sonos group state.** A Sonos that is grouped with another loses
  the ability to play an arbitrary URL until ungrouped. Slice W1 must
  surface group state in the discovery payload so the app can prompt
  the user.
- **Icecast latency variability.** Sonos buffers aggressively
  (~500–2000 ms). The startup chirp is long enough to capture the
  buffered delay, but the very first calibration cycle may need an
  expanded `MAX_ADJUSTMENT_MS`. Telemetry will tell us the typical
  range.
- **Multiple Wi‑Fi speakers fed from one Icecast mount?** Sonos players
  pulling the same URL effectively form a synchronous group; if they
  each need independent delay correction we must run **one Icecast
  mount per Wi‑Fi output** (one `pw_delay_filter` + one ffmpeg + one
  Icecast mount each). That is the design the slice plan assumes.
- **Network jitter under contention.** The Coordinator's frame-stuck
  detector should still catch Wi‑Fi stalls, but the recovery story is
  not "phase-aligned re-entry" the way BT is — it is "wait for ffmpeg
  to reconnect, then resume mute_to(1000, 50)". Validate during W2.
- **`pip install soco`.** New Python dependency on the Pi. We declare
  it in `requirements.txt` and the AGENTS.md safety default ("do not
  install without approval") binds the installation step to an
  explicit user OK at deploy time.

## 7. Done Definition

The lane is "done" when:

1. A 2-speaker mixed config (1 BT + 1 Sonos) plays in sync — measured
   inter-speaker drift below the perceptual threshold over a 30 s
   session report.
2. `Align all speakers (startup tune)` aligns both with one press.
3. The manual slider works as a fallback for Wi‑Fi when calibration is
   skipped.
4. Slice 1's session report and the Coordinator's `events.jsonl`
   include the Wi‑Fi output in their inventory. No silent gaps.
