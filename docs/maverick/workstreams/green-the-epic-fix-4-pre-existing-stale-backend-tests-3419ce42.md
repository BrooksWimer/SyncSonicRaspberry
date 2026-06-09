# Workstream: Green epic fix 4 pre-existing stale backend tests

- Workstream branch: `maverick/syncsonic/ultrasonic/green-the-epic-fix-4-pre-existing-stale-backend-tests-3419ce42`
- Durable base branch: `ultrasonic-runtime-sync`
- Lane: `ultrasonic-runtime-sync`

## 2026-06-05T19:31:34-04:00

- Scope: green `backend/measurement` tests that were stale after slice-15/18 measurement-protocol and actuator refactors.
- Changed:
  - `backend/measurement/runtime_latency_service.py`: clamped demodulated-envelope timing landmarks back to the threshold edge when correlation-derived landmarks drift outside the configured edge window.
  - `backend/measurement/analyze_lag.py`: added a NumPy FFT fallback for `scipy.signal.correlate` so measurement tests run in lightweight environments while preserving the SciPy fast path.
  - `backend/measurement/test_slice4_observer.py`: updated the stale proposal-row test to the current `ActuationResult` API and asserted `actuation_applied_ppm`.
  - `backend/measurement/test_runtime_latency_service.py`: refreshed clock-prior and independent-SNR fixtures against the current envelope landmark behavior while preserving the original behavior checks.
  - `backend/measurement/tests/test_slice5_actuator.py`: updated stale actuator tests for slice-18 behavior: median-of-5 confidence gating, 30 ms apply threshold, and per-miss amplitude escalation with success de-escalation.
- Test classification for PR body:
  | Test name | Classification | What changed and why it still verifies behavior |
  | --- | --- | --- |
  | `test_full_proposal_row_written` / now `test_full_actuation_row_written` | (a) stale test | The service no longer accepts `proposal_record`; it records current actuation through `ActuationResult`. The test now proves a complete observation row includes measured latency, confidence, pattern-state JSON, and applied actuation. |
  | `test_demodulated_envelope_pattern_uses_leading_edge_not_loudest_window` | (b) real regression | The detector could report a correlation landmark about 34 ms after threshold crossing. Production code now clamps out-of-edge-window landmarks to the envelope threshold edge, so the test still proves leading edge beats the loudest window. |
  | `test_pattern_detector_uses_clock_prior_to_avoid_late_echo_group` | (a) stale fixture | The current envelope landmarking shifted synthetic candidate timing. The fixture now matches the current detector while still proving clock prior selects the true group over the late echo group. |
  | `test_pattern_detector_has_independent_candidate_snr_floor` | (a) stale fixture | The background tone now places candidates between the 12 dB strict floor and 9 dB pattern floor. The test still proves the lower candidate SNR floor admits the pattern independently. |
- Extra stale tests found by the acceptance command:
  - `backend/measurement/tests/test_slice5_actuator.py` still asserted pre-slice-18 immediate correction and three-miss amplitude escalation. Tests now drive the five-sample confidence window and current every-miss/success-de-escalation ladder behavior.
- Verification:
  - `python3 -m pytest backend/measurement -q` -> `52 passed in 1.47s`.
  - From `backend/`: `python3 -m compileall syncsonic_ble measurement` -> pass.
- Pi validation: not run. This was a CI/test-greening slice and did not deploy, restart services, or mutate Pi/runtime config.
- Follow-up: none for this slice.
