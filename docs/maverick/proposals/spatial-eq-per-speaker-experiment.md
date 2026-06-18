# Spatial Track E: Per-Speaker EQ Through the Live Chain (2026-06-17)

Lane: `spatial-audio-awareness`  
Pi target: `syncsonic@10.0.0.89`  
Status: **go on actuation, no-go on current correction policy for product quality**

This document records the refined goal, what we built, Pi validation evidence, and what
whoever picks this up next should do.

## Refined goal (operator-aligned, 2026-06-17)

SyncSonic is an **inference engine for listening experience**, not just timing sync. The
full actuation surface for a great multi-speaker result is:

| Lever | Purpose |
|---|---|
| **Timing** | Per-speaker delay / drift correction (already proven via ultrasonic runtime) |
| **Volume** | Per-speaker level matching |
| **Stereo L/R** | Per-speaker channel role / balance |
| **EQ** | Per-speaker tonal correction at the listening position |

**Target workflow:**

1. Measure each speaker **in isolation** at the main listening position (MLP).
2. **Infer** system-wide corrections from those curves (v1: per-speaker median-referenced
   inverse EQ).
3. **Apply** corrections in the live graph without disturbing timing alignment.
4. **Validate** with an **all-speakers-on** measurement at the MLP.
5. Optional minor touch-up from the combined pass.

Success is **audible coherence** at the seat, not flat mic response in isolation.

## What shipped in this slice

### Code (backend)

| Artifact | Role |
|---|---|
| `backend/measurement/eq_measurement.py` | Log sweep → mic capture → response curve → inverse profile + baseline JSON |
| `backend/measurement/eq_apply.py` | Enable/disable per-speaker `pw_eq_filter` on the live graph |
| `backend/tools/pw_eq_filter.c` | Per-speaker stereo biquad EQ (up to 128 bands) |
| `backend/syncsonic_ble/helpers/pipewire_eq_transport.py` | Inserts EQ **after** delay filter: `virtual_out → delay → eq → sink` |

### CLI examples (on Pi, from `backend/`)

```bash
# Per-speaker isolated measurement (others muted)
python3 -m measurement.eq_measurement \
  --mac F4:6A:DD:D4:F3:C8 \
  --sink bluez_output.F4_6A_DD_D4_F3_C8.1 \
  --mic-source alsa_input.usb-Jieli_Technology_UACDemoV1.0_4150333035313213-00.mono-fallback

# Apply inferred profiles to all connected speakers
python3 -m measurement.eq_apply --all-connected

# Combined validation (all speakers, EQ on, play via virtual_out)
python3 -m measurement.eq_measurement --combined \
  --mic-source alsa_input.usb-Jieli_Technology_UACDemoV1.0_4150333035313213-00.mono-fallback

# Disable EQ and restore delay-only routes
python3 -m measurement.eq_apply --all-connected --disable
```

Runtime env: scripts bootstrap `XDG_RUNTIME_DIR=/run/syncsonic` and
`PULSE_SERVER=unix:/run/syncsonic/pulse/native` automatically.

Generated artifacts (not committed; room/speaker specific):

- `backend/eq_profiles/<MAC>.json` — inverse EQ for `pw_eq_filter`
- `backend/eq_measurements/<MAC>.baseline.json` — smoothed response curves for inference

## Pi validation evidence (2026-06-17 EDT)

**Setup:** Pi 4, USB measurement mic at operator listening position, three connected VIZIO BT
speakers (`F4:6A`, `28:FA`, `2C:FD`), `syncsonic.service` + `runtime-latency.service` active.

### Per-speaker isolated baselines (pre-EQ)

| Speaker MAC | Smoothed response span | Inverse bands |
|---|---:|---:|
| `F4:6A:DD:D4:F3:C8` | 46.9 dB | 85 |
| `28:FA:19:B6:0E:3B` | 43.7 dB | 88 |
| `2C:FD:B4:69:46:0A` | 42.4 dB | 86 |

### Apply result

All three `pw_eq_filter` processes started; `profile_band_count` matched profiles (85–88).
Verified link chain for one speaker:

```
virtual_out → syncsonic-delay-<mac> → syncsonic-eq-<mac> → bluez_output.<mac>
```

Ultrasonic delay filters remained in-path; EQ did not bypass alignment.

### Combined measurement (all speakers, EQ on)

| Mode | Response span at MLP |
|---|---:|
| `combined_all_speakers` (after per-speaker EQ) | **36.9 dB** |

Note: no **combined before-EQ** baseline was captured in this session, so this is not a
strict mic A/B — only a post-correction reference.

### Subjective result (operator)

**Clearly different, not clearly better.** The experiment validates actuation and inference
 plumbing; the **full inverse flattening policy** (85+ peaking bands per speaker) is too
 aggressive for pleasant listening through lossy BT codecs.

## Go / no-go

| Question | Verdict |
|---|---|
| Can we measure per-speaker response at the MLP? | **Go** |
| Can we apply per-speaker EQ in the live graph without breaking timing? | **Go** |
| Can we validate with an all-speakers-on pass? | **Go** |
| Does v1 full inverse EQ improve listening? | **No-go (policy)** — noticeable but not preferred |
| Are all four levers available for coordinated tuning? | **Go** — timing, volume, stereo L/R, EQ |

## Recommended next work

1. **Gentler EQ policy** — level/tilt matching + fewer bands; cap boost; optimize for
   combined curve, not isolated flatness.
2. **Capture combined before-EQ baseline** before any correction for objective A/B.
3. **Coordinate levers** — order: timing (hold) → level match → stereo roles → modest EQ.
4. **Track E conclusion feeds Track C** — capability profiles via chirp energy may be more
   codec-robust than sweep deconvolution for role assignment (see epic charter).

## Relation to other docs

- Slice 0 feasibility: `docs/maverick/workstreams/spatial-slice-0-feasibility.md`
- Epic charter (North Star / four levers): `docs/maverick/epics/spatial-audio-awareness.md`
- Ultrasonic codec wall context: `docs/maverick/proposals/06-ultrasonic-vs-inband.md`
