# Workstream: Spatial Slice 1 — EQ measure, apply, validate (2026-06-17)

- Lane: `spatial-audio-awareness`
- Branch: `spatial-audio-awareness`
- Updated: 2026-06-18
- Full record: [`../proposals/spatial-eq-per-speaker-experiment.md`](../proposals/spatial-eq-per-speaker-experiment.md)

## Summary

Completed the Slice 1 loop that Slice 0 did not specify: per-speaker EQ measurement,
per-speaker inverse inference, live apply in the PipeWire graph, and combined validation at
the listening mic. **Actuation succeeded; v1 correction policy did not pass the listening bar.**

## What changed (code)

- `eq_measurement.py` — baselines + `--combined` validation mode
- `eq_apply.py` — enable/disable EQ on connected speakers
- `pipewire_eq_transport.py` — delay → eq → sink insertion; SyncSonic runtime env for subprocesses
- `pw_eq_filter.c` — MAX_BANDS 128

## Verification

- Local: `python -m compileall syncsonic_ble measurement`; `pytest tests/test_eq_measurement.py`
- Pi (2026-06-17): three isolated sweeps, three EQ filters applied, combined span 36.9 dB,
  operator reports clearly audible change (not preferred over uncorrected)
- Services after test: `syncsonic.service` + `runtime-latency.service` active

## Follow-up

See proposal doc § Recommended next work. Do not promote full inverse EQ as product default
without a gentler policy and combined before/after A/B.
