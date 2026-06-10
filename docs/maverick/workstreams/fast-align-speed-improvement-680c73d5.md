# Workstream: fast-align speed improvement

_Status recorded by Codex on 2026-06-09T01:21:27-04:00._

- Workstream: fast-align-speed-improvement-680c73d5
- Epic: ultrasonic-runtime-sync
- Branch: maverick/syncsonic/ultrasonic/fast-align-speed-improvement-680c73d5
- Summary: Implemented fast-align confidence-window N=2 while preserving steady-state N=3, and ensured fast-align entry does not reset existing per-speaker burst amplitude ladder indices.
- Changes:
  - Added `FAST_ALIGN_CONFIDENCE_WINDOW_N = 2` in `backend/measurement/runtime_latency_service.py`.
  - Runtime service now detects `FAST_ALIGN_TRIGGER_PATH`, logs mode transitions, uses `FAST_ALIGN_CADENCE_SEC` while active, and passes the fast-align confidence window to Slice 5 only while active.
  - Slice 5 actuator now accepts an optional `confidence_window_n` argument defaulting to existing `CONFIDENCE_WINDOW_N`.
  - Added runtime service tests for fast-align window selection and amp-index preservation.
- Verification:
  - `python3 -m pytest backend/measurement/ -q` passed: 59 tests passed in 1.43s.
  - Initial sandboxed pytest attempt was blocked by `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`; rerun outside the broken sandbox with approval passed.
- Pi validation: not run. Operator requested no Pi deploy for this workstream; operator will validate on Pi after lane promote.
- Follow-up: none from local verification.
