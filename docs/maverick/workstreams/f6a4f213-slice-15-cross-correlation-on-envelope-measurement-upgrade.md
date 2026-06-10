# Slice 15: cross-correlation-on-envelope measurement upgrade

## Status

Implementation complete; verification partially blocked.

## Scope

- Add shared correlation helpers for synthetic envelope templates, FFT cross-correlation, and quadratic peak interpolation.
- Replace the demodulated-envelope landmark finder with cross-correlation peak finding while preserving the existing pattern analyzer contract.
- Surface `measurement_precision_us` in selected pattern detections and `burst_pattern_arrival` journal records.
- Extend startup lag analysis with an optional fractional lag offset while keeping integer `lag_samples`.

## Verification

- 2026-05-31 local compile: `python3 -m compileall measurement syncsonic_ble -q` from `backend/` passed.
- 2026-05-31 local pytest blocked: execution image has no `numpy`, `scipy`, or `pytest`; system Python is externally managed, `python3 -m venv` is unavailable because `ensurepip`/`python3-venv` is missing, and `sudo apt-get` is blocked by container `no new privileges`.
- 2026-05-31 Pi dependency check: `/home/syncsonic/SyncSonicPi/backend/.venv/bin/python3` has `numpy 1.24.2`, `scipy 1.10.1`, and `pytest 9.0.3`.
- 2026-05-31 Pi staging verification: rsynced backend to `/tmp/syncsonic-slice15-backend/` only; did not touch the live checkout or restart services.
- 2026-05-31 Pi staging compile: `/home/syncsonic/SyncSonicPi/backend/.venv/bin/python3 -m compileall /tmp/syncsonic-slice15-backend/measurement /tmp/syncsonic-slice15-backend/syncsonic_ble -q` passed.
- 2026-05-31 Pi staging focused tests: `cd /tmp/syncsonic-slice15-backend && /home/syncsonic/SyncSonicPi/backend/.venv/bin/python3 -m pytest measurement/tests/test_slice15_xcorr_envelope.py -v` passed, 6/6.
- 2026-05-31 Pi staging full measurement tests: `cd /tmp/syncsonic-slice15-backend && /home/syncsonic/SyncSonicPi/backend/.venv/bin/python3 -m pytest measurement/tests/ -v` failed, 14 passed / 5 failed. All failures are in `measurement/tests/test_slice5_actuator.py` and expect `corrected` where the current actuator returns `within_threshold`; no Slice 15 test failed in the full run.
- 2026-05-31 Pi read-only service inspection: `runtime-latency.service` is inactive after startup gate timeout with `reason="no_connected_speaker_macs"` and `filter_socket_macs=[]`. Live deploy/restart/listening validation not performed because the requested local/full test gate is not clean and no runtime filter sockets are present.
