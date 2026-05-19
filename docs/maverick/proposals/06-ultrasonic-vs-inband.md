# Proposal 06: Ultrasonic vs In-Band Runtime Probe Measurement

## Slice-0 Decision Harness

This slice creates the measurement harness for the first
`ultrasonic-runtime-sync` decision: whether SyncSonic should pursue
continuous ultrasonic probing or active in-band chirp probing for runtime
drift correction.

Operator decisions locked for this slice:

- Ultrasonic probe frequency: 18.5 kHz.
- Ultrasonic amplitude: full amplitude is acceptable for the current room.
- Portability: keep `--skip-ultrasonic` for environments where ultrasonic
  playback should be avoided.
- In-band mode: active stimulus. The operator briefly pauses music, injects
  the chirp, records the response, then resumes playback.

## Implemented Artifacts

- `backend/measurement/probe_signals.py` generates deterministic 48 kHz mono
  PCM probe WAVs.
- `backend/measurement/runtime_probe_compare.py` is the operator-facing CLI.
- `backend/measurement/test_runtime_probe_compare.py` covers WAV duration,
  ultrasonic FFT peak placement, and high-frequency power reporting.

Default generated files:

- `syncsonic_inband_active_chirp_48k_mono.wav`
- `syncsonic_ultrasonic_18500hz_48k_mono.wav`

Example:

```bash
python -m measurement.runtime_probe_compare --out-dir /tmp/syncsonic-runtime-probes
```

For portability-sensitive rooms:

```bash
python -m measurement.runtime_probe_compare --skip-ultrasonic
```

## Operator Pi Validation Step

Pi validation is intentionally not performed in this implementation pass. The
end-to-end harness needs the operator physically present with the three-speaker
setup on `syncsonic@10.0.0.89`.

Recommended operator sequence:

1. Copy the generated WAVs to the Pi.
2. For the in-band run, briefly pause music, start `virtual_out.monitor` and
   mic capture, play the in-band WAV through `virtual_out`, then resume music.
3. For the ultrasonic run, start the same reference and mic capture while
   playing the 18.5 kHz WAV at normal output level.
4. Bring the captures back and run:

```bash
python -m measurement.runtime_probe_compare \
  --inband-capture /path/to/inband-mic.wav \
  --ultrasonic-capture /path/to/ultrasonic-mic.wav
```

## Workstream Summary

2026-05-19:

- Added the slice-0 probe generator and comparison CLI in `backend/measurement`.
- Documented the operator-locked assumptions and Pi validation workflow here.
- Local verification expected for this pass:
  - `python -m compileall backend/measurement`
  - `python -m pytest backend/measurement/test_runtime_probe_compare.py`
- Pi validation remains the next operator step and is not claimed complete.
