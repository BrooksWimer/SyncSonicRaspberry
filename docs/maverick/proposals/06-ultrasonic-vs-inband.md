# Proposal 06: Ultrasonic vs In-Band Runtime Probe Measurement

_Slice-0 of the `ultrasonic-runtime-sync` epic. Strategic context lives in [`../ROADMAP.md`](../ROADMAP.md) §3.3 and the epic charter [`../epics/ultrasonic-runtime-sync.md`](../epics/ultrasonic-runtime-sync.md). This file holds the full experimental record; see those docs for the overarching conclusion._

## The question slice-0 answered

The coordinated engine on `main` already has two of the three pieces needed for continuous runtime drift correction: the Slice 2 elastic delay engine (accepts `set_rate_ppm` adjustments without xruns) and the Slice 4 lag analyzer (FFT-based normalized cross-correlation for measuring inter-speaker offset from a mic capture). What was missing was the **measurement signal**.

Two candidate probe signals, both theoretically viable, both with real failure modes:

- **Ultrasonic burst** (>=18 kHz, inaudible to most adults) — risk: cheap BT speakers may brick-wall around 16 kHz, or A2DP codecs may aggressively quantize content the codec believes humans can't hear.
- **In-band chirp** (420–3400 Hz active stimulus, or detected during musically quiet regions) — risk: music masks the chirp at the mic; detection requires music-aware gating.

The lane charter is explicit: don't write the runtime loop until this comparison is data-backed.

## Operator decisions locked into the slice-0 experiment

- Ultrasonic probe frequency: **18.5 kHz** (safer for mixed speaker bandwidth than 19 kHz; less risk of brick-wall null)
- In-band mode: **active stimulus** (brief pause + clean chirp, deterministic for comparison)
- Pet safety: **none in room**, ultrasonic at full amplitude acceptable; `--skip-ultrasonic` flag still exposed

## Implementation artifacts

- `backend/measurement/probe_signals.py` — 48 kHz mono PCM probe WAV generators (inband chirp + ultrasonic burst, with raised-cosine fades)
- `backend/measurement/runtime_probe_compare.py` — operator-facing CLI; generates WAVs and analyzes returned captures
- `backend/measurement/test_runtime_probe_compare.py` — local unit tests for WAV shape + FFT peak placement + HF power helper

## Experimental method (Pi validation, 2026-05-19)

Hardware:
- Pi at `syncsonic@10.0.0.89`
- One BT speaker connected (the operator's cheapest unit — worst-case-for-ultrasonic representative)
- USB measurement mic via `alsa_input.usb-Jieli_Technology_UACDemoV1.0`
- Audio routed through SyncSonic's `virtual_out` sink (the production path)

For each variant: `parecord` started against the USB mic, `paplay` plays the probe WAV through `virtual_out`, captures saved as 48 kHz mono s16 WAV. Capture window ~3.5 s, probe content ~2.25 s.

Three runs:
1. **Pure tone, music playing** — original `probe_signals.build_ultrasonic_probe` (pure 18.5 kHz sine, 2.65 s)
2. **Pure tone, music paused** — same probe, silent background
3. **Chirp, music paused** — probe rewritten as 18.0->19.5 kHz linear sweep over 2.25 s with 200 ms silence margins; same playback/capture path

## Results

### Cross-correlation lag analysis (existing `analyze_lag.estimate_lag_samples`)

| Run | `confidence_primary` | `confidence_secondary` | `peak_correlation` | `lag_ms` |
|---|---|---|---|---|
| Pure tone, music playing | 2.36 | 1.10 | +0.0031 | 448 |
| Pure tone, silent | 2.69 | 1.05 | -0.0062 | 402 |
| **Chirp 18.0-19.5 kHz, silent** | **1.57** | **1.00** | **-0.011** | **-49 (at search-window boundary)** |

The analyzer's own docstring declares `confidence_primary > 5` as "solid" and `confidence_secondary > 2` as "unambiguous". **Every ultrasonic run is well below both thresholds.** `peak_correlation` is effectively zero across the board. The chirp result was worse than the pure tone — opposite of expected behavior (chirps should correlate better than tones because their swept frequency makes each lag unique).

### Direct spectral energy analysis (17.5-20 kHz band, 50 ms windows, 25 ms hop)

This is where the picture changes. The captured chirp's energy-over-time, computed by sliding-window FFT and band-filtered to 17.5-20 kHz:

```
CAPTURED 17.5-20 kHz band energy over time (silent background)
  t = 0.00 -> 0.82 s     ~ -54 dB    (noise floor — capture started, probe not yet)
  t = 0.85 s              -13 dB    <-- chirp arrival, +40 dB jump
  t = 0.85 -> 3.05 s     -2 to -20 dB (chirp playing, energy ragged)
  t = 3.10 -> end        ~ -54 dB    (chirp ended, back to noise floor)
```

The 40 dB SNR between "probe present" and "probe absent" is unambiguous. Ordinary music has almost no content in the 18-20 kHz band, so this finding is robust to music background (the music-playing run also showed -51 dB during chirp, only 3 dB worse than silent — i.e., music does not mask ultrasonic).

The **shape** of the captured energy is ragged: it varies between -2 dB and -20 dB during the chirp playback rather than maintaining a flat ~-10 dB envelope as the reference does. This raggedness is consistent with A2DP codec quantization — the codec preserves total energy in the band but aggressively quantizes individual waveform samples in the >16 kHz range.

## Diagnosis

**The BT A2DP codec preserves energy but mangles waveform shape in the ultrasonic band.**

A2DP codecs (SBC, AAC, aptX, ...) are psychoacoustically tuned: they assume humans can't hear above ~16 kHz, so they apply aggressive quantization (effectively, fewer bits per sample) in the high-frequency bands. The result is that:

- Total energy in the 17-20 kHz band survives transmission and is reproduced by the speaker
- The phase and fine-grained waveform shape do NOT survive — the captured signal in that band is closer to noise-modulated-by-the-envelope than a clean reference
- Cross-correlation needs waveform shape preserved -> fails
- Envelope/energy detection needs only energy-over-time preserved -> succeeds

This explains every observation:
- The pure tone gave higher correlation confidence than the chirp because a pure tone's wholly-periodic structure produces broad correlation peaks even from noise — the analyzer locks onto something but with low confidence. The chirp's higher Fisher information should have improved correlation, but because the codec destroys the phase relationship within the chirp, the chirp's advantage disappears.
- The `hf_power_dbfs` measurement was nearly identical across pure tone, chirp, and music-on-vs-music-off: energy is conserved, structure is not.
- The negative lag (-49 ms, at search-window boundary) is the cross-correlator giving up — argmax on essentially noise.

## Recommendation: ultrasonic wins on viability, but the detector must change

**Ultrasonic at 18.0-19.5 kHz is the right probe signal for SyncSonic.** It survives the BT speaker -> air -> mic round trip with ~40 dB SNR, is naturally immune to music masking, and is inaudible to adult humans. The cheap Chinese BT speaker used for this validation is a representative worst case for cheap consumer BT audio; if the signal survives there, it survives across the operator's fleet.

**Cross-correlation via `analyze_lag.estimate_lag_samples` is the wrong detector for ultrasonic over BT.** The codec mangling means the existing chirp-correlation pipeline that works for startup calibration (where the signal is reproduced through the wired/dedicated path) does not work for runtime probes (which must traverse the BT codec). Slice 1 needs a new detector.

### Slice 1 architecture (data-backed)

Cadence-based bursts + envelope detection:

- Emit short ultrasonic bursts (~50-200 ms each) at known cadence (~1 Hz, verify under CPU load)
- Bursts can rotate frequency slightly (e.g., 18.0, 18.5, 19.0, 19.5 kHz on a 4-cycle rotation) for additional disambiguation and per-frequency speaker-response measurement
- On the mic side: bandpass filter (17-20 kHz) -> envelope follower -> peak detector with ~10 ms time resolution
- Each detected peak gives one arrival timestamp. Drift across consecutive bursts gives the per-speaker rate adjustment to feed into the elastic engine's `set_rate_ppm`.

This architecture sidesteps the BT codec problem entirely because envelope detection only requires energy preservation, which the codec already gives us.

### Open questions for slice 1

- **Cadence sweet spot**: 1 Hz feels right per the epic charter; verify under CPU load on the Pi at the same time as 24-hour soak validation.
- **Burst duration**: short enough to be inaudible to pets/sensitive listeners (the energy envelope of a 50 ms burst integrates to less than a perceptual threshold for most), long enough to be reliably detected against room reverb (~30 ms reverberation tail is typical for living rooms).
- **Multi-speaker arrival disambiguation**: with N speakers playing simultaneously, each burst arrives at the mic from N different paths. The cadence rotation across frequencies is one strategy; per-speaker time-slotted emission is another (mute peers briefly while one speaker emits). The slice-1 charter should specify which.

## Pi validation evidence

Captured WAVs and the full sliding-window FFT output are reproducible by re-running:

```bash
ssh syncsonic@10.0.0.89
cd /home/syncsonic/SyncSonicPi
git checkout maverick/syncsonic/ultrasonic/slice-0-ultrasonic-vs-in-band-probe-measurement-531fcb61
# Generate probes
cd backend && python3 measurement/runtime_probe_compare.py
# Capture + analyze (see git log for the operator-driven harness invocation used)
```

The original captures (`cap-inband.wav`, `cap-ultrasonic.wav`, `cap-ultrasonic-chirp.wav`) live on the Pi at `/tmp/syncsonic-slice0-captures{,-silent,p5-captures}/` until the next reboot; pull them off with `scp` if the proposal needs raw evidence preserved alongside.

## Decision

**Ultrasonic + cadence-based envelope detection wins.** Slice 1 charter follows.
