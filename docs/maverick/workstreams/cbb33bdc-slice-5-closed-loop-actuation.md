# Slice 5 - Closed-Loop Actuation Layer

Workstream branch: `maverick/syncsonic/ultrasonic/slice-5-closed-loop-actuation-with-two-stage-control-cbb33bdc`

Epic lane: `ultrasonic-runtime-sync`

## 2026-05-30 Implementation Log

- Added `backend/measurement/slice5_actuator.py` with per-speaker `WARMING_UP`, `ACTIVE`, and `SUSPENDED` state, warm-up gating, rolling miss-rate suspension, clamped-proposal skips, two-stage slider/rate actuation, SIGUSR1-capable emergency stop, env-var emergency stop, BLE stop callback registration, and `BURST_AMP_X1000 = 300`.
- Extended `backend/measurement/slice4_observer.py` with `actuation_applied_ppm` immediately after `proposed_adjustment_ppm`; missing call-site values default to `0.0`.
- Wired `backend/measurement/runtime_latency_service.py` so `--enable-correction` uses pattern mode, applies burst amplitude `0.300`, instantiates Slice 5 actuation for discovered speaker sockets, feeds relative proposals into the actuator, records applied ppm in Slice 4 CSV rows, and routes SIGUSR1 to emergency stop.
- Added `backend/measurement/tests/test_slice5_actuator.py` with the eight requested unit-test cases.
- Added one-time migration for pre-Slice-5 observation CSV headers so future rows with `actuation_applied_ppm` remain parseable by standard CSV readers.

## Verification

- `python3 -m compileall measurement syncsonic_ble` from `backend/`: passed.
- Lightweight actuator smoke check with injected socket writer: passed.
- Lightweight Slice 4 CSV header migration smoke check: passed.
- `python3 -m pytest measurement/tests/test_slice5_actuator.py measurement/test_slice4_observer.py measurement/test_runtime_latency_service.py` from `backend/`: blocked because the workspace Python does not have `pytest` installed (`No module named pytest`).
- Pi deploy: copied changed backend files and workstream note into `/home/syncsonic/SyncSonicPi` on `syncsonic@10.0.0.89`.
- Pi compile: `python3 -m compileall /home/syncsonic/SyncSonicPi/backend/measurement /home/syncsonic/SyncSonicPi/backend/syncsonic_ble` passed.
- Pi pytest: blocked because the Pi Python also does not have `pytest` installed (`No module named pytest`).
- Pi warm-up/rate gate: started `runtime-latency.service` with `--enable-correction --slice4-observe --slice4-observation-path /tmp/slice5-acceptance-cbb33bdc.csv` after resetting filters to calibrated delays (`28:FA:19:B6:0E:3B` = 6964 samples / 145.083 ms, `45:7A:D9:00:81:19` = 5475 samples / 114.062 ms). CSV showed `actuation_applied_ppm=0.0` during warm-up and then nonzero rate actuation once active, including `7.149305554727713` ppm for `28:FA:19:B6:0E:3B` at `2026-05-30T22:40:06Z` and `5.4965277786056195` ppm for `45:7A:D9:00:81:19` at `2026-05-30T22:40:22Z`.
- Pi slider stage: live run naturally crossed the slider threshold for `45:7A:D9:00:81:19`; logs showed `slice5_slider_adjustment` with `residual_ms=-5.75`, `target_delay_samples=5199`, and successful socket response.
- Pi SIGUSR1 escape: sent `kill -USR1 17516`; both filter sockets reported `rate_ppm=0` within the 0.5 s check, and logs showed `slice5_emergency_stop` with `elapsed_sec=0.0012266680005268427`.
- Pi env-var escape: started validation with `MAVERICK_CORRECTION_STOP=1`; next measured cycle logged `slice5_emergency_stop`, both sockets reported `rate_ppm=0`, and `/tmp/slice5-envstop-cbb33bdc.csv` rows had `actuation_applied_ppm=0.0`.

## Pending

- Run the pytest suite in an environment with pytest installed.
- Full deliberate 300 ms desync gate remains pending. The slider path was exercised by a natural residual threshold crossing, but the requested manual 300 ms offset/recovery timing was not completed.
- 30-minute operator listening gate remains pending; this requires the human operator to judge audible artifacts during music playback.
