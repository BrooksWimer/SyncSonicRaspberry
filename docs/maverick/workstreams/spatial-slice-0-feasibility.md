# Spatial-audio-awareness Slice 0 Feasibility

Date: 2026-06-10
Lane: `spatial-audio-awareness`
Scope: documentation-only feasibility study for Pi 4 + one USB measurement microphone + three speakers.

## Context

The spatial-audio-awareness charter says the coordinated engine currently sends the same stereo mix to every speaker, and frames this lane as exploratory, low priority, and explicitly not in competition with `ultrasonic-runtime-sync` or MVP hardening (`docs/maverick/epics/spatial-audio-awareness.md`). The charter's first requested slice is exactly this feasibility question: with Pi 4 hardware, one USB mic, and three speakers, which spatial features are physically measurable and which are research.

The durable hardware baseline in project memory is Pi 4 + four USB Bluetooth controllers + one USB measurement microphone, optionally one Wi-Fi Sonos (`docs/maverick/PROJECT_MEMORY.md`). Raspberry Pi's current Pi 4 specification confirms Bluetooth 5.0/BLE, two USB 3.0 ports, two USB 2.0 ports, and two micro-HDMI ports for display output; it does not list HDMI input or ARC/eARC capture hardware. Source: https://www.raspberrypi.com/products/raspberry-pi-4-model-b/specifications/

## Verdict Table

| Lane | Verdict | One-line reason |
|---|---|---|
| Per-speaker channel routing | MEASURABLE NOW | The current graph already has one stereo filter per speaker and per-channel sink volume; the missing piece is explicit placement/role metadata and a real channel-selection/mix matrix. |
| Microphone-driven room geometry | MEASURABLE WITH SMALL HW ADDITION | One mic can measure arrival time/range-to-mic after transport latency is controlled, but cannot infer 2D/3D speaker bearing or unique room geometry from one static mic position. |
| Listener-position awareness | PURE RESEARCH | BLE RSSI exists but is intentionally too noisy for direct actuation; UWB needs extra radios; phone audio TOA needs a new phone-side measurement protocol and clock/permission work. |
| Spatial format support | PURE RESEARCH | Current signal flow is stereo-in/stereo-out-per-speaker; surround/Atmos needs multichannel input, decode/render, channel-role metadata, and a mix matrix. |
| Home theater HDMI ARC/eARC input | DEAD END | Pi 4 exposes micro-HDMI output ports, not HDMI ARC/eARC input; this needs external capture/receiver hardware or a different platform. |

## Existing Measurement And Routing Primitives

The runtime ultrasonic stack has the primitives needed for per-speaker time-of-arrival measurement:

- `backend/tools/pw_delay_filter.c` is one process per speaker with `input_FL`, `input_FR`, `output_FL`, and `output_FR` PipeWire DSP ports. It maintains stereo delay rings and accepts control commands over a Unix socket.
- `backend/tools/pw_delay_filter.c` accepts `emit_burst <freq_hz_x10> <duration_ms> <amplitude_x1000>`, synthesizes the burst in both output channels, and records emitted frame indices in `query_emit_timestamps`.
- `backend/syncsonic_ble/helpers/pipewire_transport.py` wires `virtual_out:monitor_FL/FR` through the per-speaker delay filter to the speaker sink's `playback_FL/FR`, and exposes `emit_burst()` with default `freq_hz=18500.0`.
- `backend/syncsonic_ble/helpers/arrival_burst_actuation.py` is the thin Python wrapper around `emit_burst` and `query_emit_timestamps`; it delegates actual burst synthesis and timestamping to the filter.
- `backend/measurement/runtime_latency_service.py` captures the USB mic as mono 48 kHz PCM via `parecord`, scans the mic stream around expected arrival windows, and logs `burst_pattern_arrival` with `latency_ms`, SNR, matched sample indices, and `measurement_precision_us`.
- `backend/measurement/correlation.py` provides the demodulated-envelope template/correlation helpers used by the runtime detector.
- `backend/measurement/slice5_actuator.py` implements the current confidence-gated actuation: adaptive per-speaker burst amplitude ladder `(300, 600, 950)`, median-window correction, 30 ms apply threshold, and sigma-floor gating.
- `deploy/runtime-latency.service` starts `runtime_latency_service.py` in pattern mode with `--pattern-bursts 3`, `--pattern-gap-ms 300`, `--amplitude 0.3`, and `--cadence-sec 5`.

The requested "30-second sparse cadence" is historical rather than current code. `backend/measurement/runtime_latency_service.py` still has `DEFAULT_CADENCE_SEC = 15.0`, `deploy/runtime-latency.service` overrides it to `--cadence-sec 5`, and `docs/maverick/PROJECT_MEMORY.md` records the June 8 promotion change from 15 seconds to 5 seconds. Older project memory also records a 15-second per-speaker cadence producing a 30-second full cycle for two speakers.

The current audio graph is stereo fan-out, not spatial routing:

- `backend/syncsonic_ble/helpers/pulseaudio_helpers.py` creates `virtual_out` as the shared default null sink and routes phone A2DP input into `virtual_out`.
- `backend/syncsonic_ble/helpers/pipewire_transport.py` connects the same `virtual_out` stereo monitor ports to every per-speaker filter.
- `backend/syncsonic_ble/helpers/pipewire_control_plane.py` stores `left_percent` and `right_percent` through `publish_output_mix()`.
- `backend/syncsonic_ble/helpers/pipewire_actuation_daemon.py` consumes that control state and passes `left_percent`/`right_percent` into `ensure_route()`.
- `backend/syncsonic_ble/helpers/pipewire_transport.py` applies those values with `pactl set-sink-volume <sink> <left>% <right>%`, so `publish_output_mix()` is consumed by the runtime actuation daemon. It is not a storage-only stub, and it is not consumed directly by the C filter in `backend/tools/pw_delay_filter.c`.

## 1. Per-speaker Channel Routing

Verdict: MEASURABLE NOW.

What is measurable now: per-speaker left/right channel selection or weighting. The graph already has stable per-speaker FL/FR ports in `backend/tools/pw_delay_filter.c`, deterministic `pw-link` wiring in `backend/syncsonic_ble/helpers/pipewire_transport.py`, and per-speaker left/right sink volume controlled through `publish_output_mix()` and `PipeWireActuationDaemon`. A reviewer can check the path from BLE volume request to route actuation in `backend/syncsonic_ble/state_change/action_request_handlers.py`, `backend/syncsonic_ble/helpers/pipewire_control_plane.py`, `backend/syncsonic_ble/helpers/pipewire_actuation_daemon.py`, and `backend/syncsonic_ble/helpers/pipewire_transport.py`.

What engine-side selection requires: the current graph only distinguishes stereo left and right. Assigning a speaker to "front-left" or "rear-right" needs a role-to-channel policy above the existing left/right balance. For a stereo source, the smallest engine change would be to treat a placement role as an input-channel selector: left speaker gets left input, right speaker gets right input, center/mono role gets an L+R sum, and rear roles are placeholders until surround input exists. A real surround system needs a general N-input to M-output mix matrix, not just `left_percent`/`right_percent`.

What the app needs: the app needs durable placement awareness keyed by speaker identity. The current frontend contains BLE/device state and per-speaker configuration surfaces, but no room-layout or channel-role model was found in `frontend/app/SpeakerConfigScreen.tsx`, `frontend/hooks/useBLE.ts`, or `frontend/utils/ble_constants.ts`. The app's smallest useful representation is a per-speaker role enum such as `left`, `right`, `mono`, `front_left`, `front_right`, `rear_left`, `rear_right`, and `center`, with `front_*` roles initially mapped to stereo left/right only.

Smallest demonstrable step: a stereo channel-role proof, not room geometry. Add a docs-backed Slice 1 implementation that assigns one connected speaker to left-only and another to right-only, then plays a stereo test file with isolated left/right content. Verification criterion: `pw-link -l` still shows each speaker routed through its own `syncsonic-delay-*` node, the control state shows different channel roles per MAC, and a mic capture or operator-readable log confirms the left-only test tone is emitted only by the left-role speaker while the right-only tone is emitted only by the right-role speaker.

## 2. Microphone-driven Room Geometry

Verdict: MEASURABLE WITH SMALL HW ADDITION.

What one static mic can measure: one static microphone can measure a scalar arrival time per speaker. With a known emitted frame and a known or controlled transport latency, that scalar becomes range to the mic:

`distance_m = speed_of_sound_m_per_s * acoustic_time_s`

At 48 kHz, one sample is 20.833 us. At roughly 343 m/s, one sample corresponds to about 7.15 mm of acoustic path. The runtime service's own precision estimate is based on correlation peak width and is reported as `measurement_precision_us` in `backend/measurement/runtime_latency_service.py`; project memory records empirical sub-millisecond precision for the promoted runtime-ultrasonic path (`docs/maverick/PROJECT_MEMORY.md`). Sub-millisecond timing is enough to resolve tens of centimeters of distance, assuming transport latency is separated from acoustic propagation.

What one static mic cannot measure: a single range from one fixed mic constrains each speaker to a circle in 2D or a sphere in 3D around the mic. It does not determine bearing. Three speakers produce three separate ranges, not a coordinate system, unless the speaker locations are constrained by human-entered placement or the mic is moved to known positions. This is geometry, not a code limitation.

What blocks direct reuse of existing TOA for geometry: `backend/measurement/runtime_latency_service.py` currently computes `latency_ms` from burst emission to mic arrival, and `backend/measurement/slice5_actuator.py` uses that total latency for alignment. That total contains filter delay, Bluetooth/Wi-Fi codec buffering, speaker DSP latency, air propagation, and detector timing. The runtime code can subtract current filter delay for dynamic target baselines, but it does not independently estimate per-speaker codec/DSP latency versus acoustic distance. Without that separation, a far speaker and a slow codec path can look identical.

What a second mic or mic relocation adds: two simultaneous mics with a known baseline give time-difference-of-arrival hyperbolas and a bearing constraint; three or more known mic positions can triangulate in 2D/3D. A single USB mic can also be relocated to several known positions, but then the operator must mark mic coordinates and keep the speaker/transport latency stable across measurements. That is a small hardware or workflow addition, not current automatic room mapping.

Conclusion: reuse the existing burst and mic measurement for "speaker acoustic range relative to a marked mic position" experiments. Do not claim automatic room geometry with one static mic.

## 3. Listener-position Awareness

Verdict: PURE RESEARCH.

BLE RSSI: physically plausible only as a coarse proximity/stability signal, not as listener coordinates. The backend already samples speaker RSSI once per second using `hcitool` and rolling medians in `backend/syncsonic_ble/telemetry/samplers/rssi_sampler.py`. That module explicitly records that single-shot RSSI readings in the same physical configuration varied from -14 dBm to -25 dBm. The coordinator deliberately uses RSSI only as a sensitivity amplifier for frame starvation, not an independent trigger, because RSSI-only policy oscillated in production (`backend/syncsonic_ble/coordinator/coordinator.py`). This makes BLE RSSI a dead end for direct listener-position actuation on current hardware.

UWB: physically plausible with additional hardware, not on the current Pi 4. The Pi 4 spec lists Bluetooth 5.0/BLE, Wi-Fi, Ethernet, USB, GPIO, and micro-HDMI; it does not include a UWB radio. A UWB-based listener-position slice would require a phone with UWB APIs plus a Pi-attached UWB anchor or custom hardware. That belongs under `custom-hardware-design`, not a Pi 4 docs-only spatial slice.

Phone audio TOA: physically plausible in principle, but pure research in this repo today. The existing phone path is audio ingress from phone A2DP source into `virtual_out` (`backend/syncsonic_ble/helpers/pulseaudio_helpers.py`) and a reserved adapter control-plane rule that says the phone is not a speaker (`backend/syncsonic_ble/utils/constants.py`, `docs/maverick/PROJECT_MEMORY.md`). Using the phone as an acoustic probe would need a phone app that emits or records calibration audio with known timing, permissions for speaker/mic capture, echo/latency calibration for phone hardware, and a protocol to exchange timestamps with the Pi. None of that exists in `frontend/` or the BLE protocol today.

Conclusion: listener-position awareness should be deferred. It is not a measurable Slice 1 on Pi 4 + one USB mic.

## 4. Spatial Format Support

Verdict: PURE RESEARCH.

Current signal flow: SyncSonic receives phone stereo audio into `virtual_out`, then fans that same stereo signal to one stereo delay filter per speaker (`backend/syncsonic_ble/helpers/pulseaudio_helpers.py`, `backend/syncsonic_ble/helpers/pipewire_transport.py`, `backend/tools/pw_delay_filter.c`). The filter operates on two channels, and the control plane stores only delay, rate, active state, and left/right percentages (`backend/syncsonic_ble/helpers/pipewire_control_plane.py`).

Missing primitives:

- Multichannel input capture or file/stream ingestion beyond phone stereo A2DP.
- Decode/demux layer that exposes 5.1/7.1 channels or object metadata.
- A role-aware output model in the app and BLE protocol.
- An engine-side mix matrix from decoded channels/objects to arbitrary speaker outputs.
- Per-output gain/delay rules that coexist with current alignment and ultrasonic actuation.
- Test assets and verification harnesses for surround channel identity.

Decoder and licensing reality: FFmpeg documents audio decoders for AC-3 and other codecs, and can be used for ordinary channel-based decode experiments (source: https://ffmpeg.org/ffmpeg-codecs.html). Dolby Atmos is an object/metadata rendering system, and Dolby's own Renderer product is commercial Mac/Windows professional software that renders beds/objects and metadata to playback layouts (source: https://professional.dolby.com/product/dolby-atmos-content-creation/dolby-atmos-renderer/). That is not the same as an open, Pi-ready, redistributable Atmos renderer. Dolby's consumer page also frames Atmos playback as requiring Atmos-created content and a device that can play back Atmos content (source: https://www.dolby.com/technologies/dolby-atmos/).

Rough engineering lift:

- Channel-based 5.1 proof from local files, downmixed to three assigned speakers: 4-8 engineering weeks after channel-role routing exists.
- Production-ish surround from phone/TV input: multiple months because input transport, user controls, and verification are missing.
- True Atmos/object rendering on Pi: defer indefinitely unless licensing, SDK access, and product strategy justify it.

Conclusion: start with stereo role routing. Do not start with Atmos.

## 5. Home Theater HDMI ARC/eARC Input

Verdict: DEAD END on Pi 4 alone.

Pi 4 hardware reality: Raspberry Pi lists two micro-HDMI ports with up to 4Kp60 support as video/sound output, plus a 4-pole stereo audio/composite port; it does not list HDMI input, ARC, or eARC capture. Source: https://www.raspberrypi.com/products/raspberry-pi-4-model-b/specifications/

Why this matters: ARC/eARC is not just another software source. It depends on HDMI physical/link behavior, CEC/ARC negotiation, and audio return from a TV into the audio processor. The current SyncSonic input path is phone A2DP into `virtual_out` (`backend/syncsonic_ble/helpers/pulseaudio_helpers.py`), not HDMI capture. There is no HDMI input module, CEC ARC stack, or lip-sync feedback loop in the repo.

Feasible alternatives:

- Use an external HDMI ARC/eARC audio extractor that outputs USB audio, S/PDIF, or analog into the Pi. Then SyncSonic would treat it as a new audio input device.
- Use a different hardware platform with HDMI input/ARC/eARC support.
- Keep TV/home-theater work in a separate future lane; it is outside the current Bluetooth-speaker product shape documented in `docs/maverick/PROJECT_CONTEXT.md`.

Conclusion: Pi 4-only ARC/eARC is a dead end. External hardware could make TV audio input feasible, but that is no longer "Pi 4 + one USB mic + three speakers."

## Slice 1 Recommendation

Recommendation: go on one bounded Slice 1 only if the operator wants a measurable spatial step now: per-speaker stereo channel-role routing.

Highest-value measurable feature: assign speaker roles for stereo left/right/mono output and prove that SyncSonic can intentionally send different stereo content to different speakers without changing ultrasonic-runtime-sync.

Concrete verification criterion:

1. With two or three connected speakers, configure at least one speaker as `left` and one as `right`.
2. Play a stereo test signal whose left and right channels alternate.
3. Verify from control state and PipeWire links that each speaker still routes through its own `syncsonic-delay-*` filter.
4. Verify by mic capture or clear operator-observed evidence that left-channel content is audible only from the left-role speaker and right-channel content only from the right-role speaker.
5. Confirm `runtime-latency.service` and ultrasonic alignment behavior are not modified by the slice.

No-go for Slice 1: automatic room geometry, listener tracking, Atmos, and HDMI ARC/eARC. Those are either not identifiable with one static mic, require new hardware, or require new product-level signal-flow foundations.
