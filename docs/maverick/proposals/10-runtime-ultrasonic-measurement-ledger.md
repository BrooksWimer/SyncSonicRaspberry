# Proposal 10: Runtime Ultrasonic Measurement Ledger

_Detailed evidence ledger for the slice 3 follow-up measurement work in the
`ultrasonic-runtime-sync` lane. `PROJECT_MEMORY.md` should point here rather
than restating test logs, commands, and per-run metrics._

## Why this ledger exists

Slice 3 added closed-loop drift correction around the slice 2 measurement
stream, but the first long Pi run produced unstable `current_codec_ms` values
while Brooks reported that music sounded stable throughout. That mismatch
changed the immediate problem from "tune the controller" to "tighten the
measurement definition before actuating."

The important proof boundary:

- Slice 2 proved large-signal relative tracking. Moving filter delays across
  131-1800 ms produced measured latency slopes of `1.004` and `1.009`, where
  `1.000` is perfect tracking.
- Slice 2 did not prove steady-state burst timestamps were precise to
  `+/-10 ms`. Its closeout already recorded `22-34 ms` per-speaker spread and
  called out 25 ms FFT-hop quantization.
- Slice 3 reused the same `EnvelopeDetector`, `WINDOW_MS=50.0`,
  `HOP_MS=25.0`, expected-arrival window, and latency calculation. It added
  `DriftController` and `set_rate_ppm` calls around that stream.

The likely disconnect: the legacy detector is a peak-energy window picker, not
a true onset / time-of-arrival estimator. It scans the detection interval and
returns the center timestamp of the loudest 50 ms FFT window. Because the burst
is 100 ms long, several valid windows can sit inside one physical burst, and
small envelope/codec/noise changes can move the selected "arrival" between
adjacent 25 ms hop buckets while audible playback remains stable.

## Doctrine / provenance note

This work was initially staged manually on `codex/ultrasonic-sample-clock-pattern`.
That branch is not a Maverick-compliant canonical workstream branch. Before
promotion, replay or squash the relevant commits onto a proper
`maverick/syncsonic/ultrasonic-runtime-sync/<slice-slug>-<id>` workstream branch
and merge that branch into `ultrasonic-runtime-sync`. Do not merge the `codex/*`
branch directly.

## Slice 3b: Sample-clock + pattern measurement

Request: build the measurement path Brooks sketched after the slice 3 validation
mismatch: exact filter-side burst timing, continuously indexed mic capture, and
a burst-pattern detector before more controller tuning.

Scope:

- Keep the existing peak detector as the default so slice 2/3 invocations remain
  backwards-compatible.
- Add mic sample indexing to `RingBuffer` so every detection can report
  `arrival_sample_index` in the continuous capture stream.
- Use the filter's existing `query_emit_timestamps` `frame_index` as emitted
  burst clock evidence and log baseline-subtracted `sample_clock_drift_ms`.
- Add optional detector modes:
  - `peak`: legacy loudest 50 ms window center.
  - `onset`: first high-frequency threshold crossing using a shorter 10 ms
    window / 2.5 ms hop.
  - `pattern`: emit multiple bursts, read exact emitted frame indices, then
    match observed mic onset spacing to emitted-frame spacing.
- Keep pattern mode measurement-only. Do not feed pattern results into
  `DriftController` until Pi logs prove the sample-clock drift signal is stable.

Out of scope:

- UX changes, BLE toggles, third-speaker scaling, or 24-hour soak.
- Controller gain changes.
- PipeWire/Pulse clock-provider refactors. This branch logs evidence needed to
  decide whether deeper clock alignment is necessary.

Local implementation state:

- `backend/measurement/runtime_latency_service.py` tracks mic sample indices and
  exposes `--detector-mode {peak,onset,pattern}`, `--pattern-bursts`,
  `--pattern-gap-ms`, and `--pattern-tolerance-ms`.
- `backend/measurement/test_runtime_latency_service.py` covers the new pure
  detector math.
- Local verification passed: `python -m compileall syncsonic_ble measurement`;
  `python -m pytest measurement -v` with 17 tests.

### Pi smoke at `6d6bf26`

Setup performed by Codex:

- Pi checkout switched to `codex/ultrasonic-sample-clock-pattern` and
  fast-forwarded to `6d6bf26`.
- `syncsonic.service` stayed active; transient `runtime-latency.service` was
  stopped after the smoke run.
- Pi still had pre-existing untracked artifacts:
  `SyncSonic Reliable Alignment Actuation for Multi-Speaker Playback.pdf` and
  `backend/tools/pw_delay_filter`.
- Pi compile passed for
  `/home/syncsonic/SyncSonicPi/backend/measurement/runtime_latency_service.py`
  and `test_runtime_latency_service.py`. Pi-side pytest was not run because
  `pytest` is not installed on the Pi.

Validation command:

```bash
sudo systemd-run --unit=runtime-latency --uid=syncsonic --gid=syncsonic \
  --working-directory=/home/syncsonic/SyncSonicPi \
  --setenv=PULSE_SERVER=unix:/run/syncsonic/pulse/native \
  --setenv=RESERVED_HCI=hci3 \
  --setenv=RESERVED_ADAPTER_MAC=2C:CF:67:CE:57:91 \
  --setenv=PYTHONUNBUFFERED=1 \
  python3 /home/syncsonic/SyncSonicPi/backend/measurement/runtime_latency_service.py \
  --max-speakers 2 --detector-mode pattern --pattern-bursts 3 --duration-ms 50 --pattern-gap-ms 300
```

Run window: `runtime-latency.service` active starting 2026-05-27 22:48:28 EDT.
The unit was stopped afterward with `sudo systemctl stop runtime-latency`.

Evidence summary:

- Event counts: `burst_pattern_emit=21`, `burst_pattern_arrival=5`,
  `burst_pattern_missed=1`, `detector_warmup=1`, `device_discovery=4`,
  `mic_capture_started=1`, `mic_capture_stopped=1`, `service_starting=1`,
  `service_stopped=1`.
- The count includes one final three-burst pattern interrupted by stopping the
  transient unit before it produced either an arrival or missed-pattern outcome.
- `28:FA:19:B6:0E:3B`: 3 pattern arrivals, SNR min/avg `12.52/12.89 dB`,
  max/avg `pattern_mean_abs_error_ms=2.06/1.20`, max absolute per-burst matched
  error `4.83 ms`, `sample_clock_drift_ms` min/max/final `-5.25/45.17/-5.25`,
  candidate count range `3..7`.
- `F4:6A:DD:D4:F3:C8`: 2 pattern arrivals and 1 missed pattern, SNR min/avg
  `12.00/12.33 dB`, max/avg `pattern_mean_abs_error_ms=2.06/1.28`, max absolute
  per-burst matched error `5.50 ms`, `sample_clock_drift_ms` min/max/final
  `-6.77/0.00/-6.77`, candidate count range `3..8`.

Acceptance result: partial.

- Pass: pattern mode executed end-to-end on the Pi against both live speakers.
- Pass: logs include exact filter `emit_frame_index` values, continuous mic
  `arrival_sample_index` values, matched arrival sample indices,
  `sample_clock_delta_ms`, `sample_clock_drift_ms`, and
  `pattern_mean_abs_error_ms`.
- Pass: successful pattern arrivals had internal spacing error under 10 ms.
- Not yet pass: one `pattern_not_matched` event on `F4:6A:DD:D4:F3:C8`.
- Not yet pass: `28:FA:19:B6:0E:3B` showed one `sample_clock_drift_ms` outlier
  at `45.17 ms`, so the run did not prove the signal correction-grade.

Planning implication: exact filter-frame emission timing plus continuous mic
sample indices removed the 50 ms / 25 ms legacy peak-window ambiguity from
successful pattern matches, but pattern acquisition needed hardening before any
controller connection.

### Continuity-gated pass at `6621741`

Design correction: spacing alone is not enough. A whole matched sequence can
still land on a late burst/echo landmark and jump the sample-clock delta by
tens of milliseconds while preserving good internal spacing.

Matcher changes:

- Find all candidate groups whose observed mic onset spacing matches exact
  filter `emit_frame_indices`.
- Once a speaker has a previous accepted sample-clock delta, choose only groups
  whose mean `arrival_sample_index - emit_frame_index` stays within
  `--pattern-clock-tolerance-ms` of the last accepted cycle.
- Compute sample-clock delta as the mean delta across the whole matched burst
  sequence instead of only the first onset.
- Pattern miss logs include candidate count, match count, best unprioritized
  spacing error, best unprioritized sample-clock error, and whether a prior was
  reset after repeated clock-prior mismatches.
- Add independent `--pattern-min-snr-db` candidate floor, default `9.0 dB`.

Local verification passed: `python -m compileall syncsonic_ble measurement`;
`python -m pytest measurement -v` with 20 tests.

Pi setup: checkout fast-forwarded to `6621741`; Pi compile passed for the
runtime service and test file. Pi-side pytest was not run because `pytest` is not
installed.

Command:

```bash
sudo systemd-run --unit=runtime-latency --uid=syncsonic --gid=syncsonic \
  --working-directory=/home/syncsonic/SyncSonicPi \
  --setenv=PULSE_SERVER=unix:/run/syncsonic/pulse/native \
  --setenv=RESERVED_HCI=hci3 \
  --setenv=RESERVED_ADAPTER_MAC=2C:CF:67:CE:57:91 \
  --setenv=PYTHONUNBUFFERED=1 \
  python3 /home/syncsonic/SyncSonicPi/backend/measurement/runtime_latency_service.py \
  --max-speakers 2 --detector-mode pattern --pattern-bursts 3 --duration-ms 50 \
  --pattern-gap-ms 300 --pattern-clock-tolerance-ms 20 --pattern-min-snr-db 9
```

Run window started 2026-05-27 23:44:05 EDT. `runtime-latency.service` was
stopped afterward; `syncsonic.service` remained active.

Evidence summary:

- Event counts: `burst_pattern_emit=30`, `burst_pattern_arrival=10`,
  `burst_pattern_missed=0`, `service_stopped=1`.
- `28:FA:19:B6:0E:3B`: 5 arrivals; selection `best_spacing=1`,
  `clock_prior=4`; SNR min/avg `9.02/9.24 dB`; max matched error `8.67 ms`;
  max pattern clock spread `8.67 ms`; max `|pattern_clock_prior_error_ms|=9.97`;
  max `|sample_clock_drift_ms|=18.92`; total rejected alternate clock groups `2`.
- `F4:6A:DD:D4:F3:C8`: 5 arrivals; selection `best_spacing=1`,
  `clock_prior=4`; SNR min/avg `9.38/9.86 dB`; max matched error `6.17 ms`;
  max pattern clock spread `6.17 ms`; max `|pattern_clock_prior_error_ms|=7.44`;
  max `|sample_clock_drift_ms|=15.81`; total rejected alternate clock groups `3`.

Acceptance result: partial.

- Pass: exact filter-frame + mic-sample pattern mode ran end-to-end for both
  speakers.
- Pass: lower candidate floor eliminated the `F4` no-candidate misses in this
  smoke run.
- Pass: the clock prior rejected alternate sequence groups and prevented the
  previous 45 ms-style jump.
- Not yet pass: baseline-relative `sample_clock_drift_ms` still exceeded 10 ms
  on both speakers.

Planning implication: keep the exact emit-frame/pattern architecture but refine
the mic-side landmark. Do not feed pattern mode into `DriftController` yet.

## Slice 3c: Demodulated envelope landmark

Design correction: pattern mode now has a demodulated ultrasonic envelope
landmark. Instead of timing the first FFT-window threshold crossing, the detector
mixes mic samples down at the configured carrier (`--freq-hz`, default
18.5 kHz), low-passes the complex baseband signal, smooths the magnitude
envelope, and uses the leading-edge slope as the mic-side arrival landmark.
Pattern matching, exact filter `emit_frame_indices`, sample-clock averaging
across the full burst sequence, and clock-prior gating remain in place.

Compatibility:

- Legacy `peak` and `onset` detector modes are unchanged.
- Pattern mode defaults to `--pattern-landmark envelope`.
- `--pattern-landmark onset` remains for A/B comparison against the previous
  threshold-crossing path.

Local intent: reduce low-SNR landmark movement inside the burst envelope while
preserving the zero-miss acquisition improvement from the `--pattern-min-snr-db 9`
pass. Still measurement-only; pattern results do not feed `DriftController`.

Local verification passed: `python -m compileall syncsonic_ble measurement`;
`python -m pytest measurement -v` with 21 tests.

### Pi smoke at `cc777b0`

Pi checkout fast-forwarded to `cc777b0`; Pi compile passed for the runtime
service and test file. Pi-side pytest was not run. The transient
`runtime-latency.service` was stopped after the run; `syncsonic.service`
remained active. The Pi still had the pre-existing untracked artifacts.

Command:

```bash
sudo systemd-run --unit=runtime-latency --uid=syncsonic --gid=syncsonic \
  --working-directory=/home/syncsonic/SyncSonicPi \
  --setenv=PULSE_SERVER=unix:/run/syncsonic/pulse/native \
  --setenv=RESERVED_HCI=hci3 \
  --setenv=RESERVED_ADAPTER_MAC=2C:CF:67:CE:57:91 \
  --setenv=PYTHONUNBUFFERED=1 \
  python3 /home/syncsonic/SyncSonicPi/backend/measurement/runtime_latency_service.py \
  --max-speakers 2 --detector-mode pattern --pattern-landmark envelope \
  --pattern-bursts 3 --duration-ms 50 --pattern-gap-ms 300 \
  --pattern-clock-tolerance-ms 20 --pattern-min-snr-db 9
```

Run window started 2026-05-28 13:36:52 EDT.

Evidence summary:

- Event counts: `burst_pattern_emit=51`, `burst_pattern_arrival=16`,
  `burst_pattern_missed=0`, `service_stopped=1`.
- `28:FA:19:B6:0E:3B`: 8 arrivals; selection `best_spacing=1`,
  `clock_prior=7`; SNR min/avg `20.18/25.92 dB`; envelope peak SNR min/avg
  `26.83/30.77 dB`; max matched error `6.65 ms`; max pattern clock spread
  `6.65 ms`; max `|pattern_clock_prior_error_ms|=6.08`; max
  `|sample_clock_drift_ms|=23.42`; drift trend approximately `-6.50 ms/min`
  (`-108 ppm` equivalent).
- `F4:6A:DD:D4:F3:C8`: 8 arrivals; selection `best_spacing=1`,
  `clock_prior=7`; SNR min/avg `21.23/25.71 dB`; envelope peak SNR min/avg
  `25.47/30.03 dB`; max matched error `4.75 ms`; max pattern clock spread
  `4.75 ms`; max `|pattern_clock_prior_error_ms|=6.36`; max
  `|sample_clock_drift_ms|=28.61`; drift trend approximately `-8.12 ms/min`
  (`-135 ppm` equivalent).

Acceptance result: partial.

- Pass: envelope landmark mode ran end-to-end for both live speakers.
- Pass: zero pattern misses across 16 arrivals.
- Pass: acquisition SNR improved sharply compared with the previous
  threshold-onset pass.
- Pass: intra-pattern geometry stayed inside the 10 ms measurement target, with
  bounded clock-prior corrections.
- Not yet pass: baseline-relative `sample_clock_drift_ms` still exceeded 10 ms
  on both speakers over the 4 minute smoke run.

Planning implication: the remaining error no longer looks primarily like "we
cannot find the burst." The envelope landmark found complete sequences with high
SNR and tight internal spacing, but both speakers still showed a same-direction
negative slope over time. That points at clock-domain alignment: mic sample
indices and PipeWire filter-frame emit indices are both nominally 48 kHz, but
they are not proven to be the same clock over minutes.

## Slice 3d: Common-mode subtraction + observe-only proposals

Design correction: the listener cares about relative speaker timing, not whether
every speaker drifts together against the mic capture process clock. Pattern-mode
sample-clock deltas now have a pure `RelativeDriftEstimator` that baselines each
speaker, computes a recent group common-mode drift from active peers, subtracts
it, and emits observe-only `relative_correction_proposed` /
`relative_correction_skipped` records. It does not call `set_rate_ppm`.

New CLI flags:

- `--enable-relative-proposals` turns on observe-only logs for pattern mode.
- `--relative-gain-ppm-per-ms` defaults to `5.0`.
- `--relative-peer-max-age-sec` defaults to `90.0`.

Legacy `--enable-correction` remains unchanged and should not be used with
pattern mode until the relative proposal stream is Pi-validated.

Offline replay against the 2026-05-28 13:36:52 EDT Pi smoke:

- No new live run was performed.
- Replaying 16 existing `burst_pattern_arrival` rows through the new estimator
  produced 1 initial `insufficient_recent_peers` skip and 15 proposals with
  `smoothing_window=1`.
- Absolute per-speaker baseline drift max: `28.611 ms`.
- Group-relative residual max: `3.312 ms`.
- Proposed ppm range: `-4.115..16.562`.
- Final common clock slope: `-108.190 ppm`.
- With runtime default `smoothing_window=5`, the same rows produced 8
  warmup/peer skips and 8 proposals; max smoothed residual `2.566 ms`; proposed
  ppm range `-0.917..12.830`.

This supports Brooks's read: most of the previously alarming same-direction
drift is common-mode and should not drive per-speaker actuation.

Local verification passed:

- `python -m compileall syncsonic_ble measurement`
- `python -m pytest measurement -v` with 25 tests
- `python measurement/runtime_latency_service.py --help` shows the observe-only
  flags

## Current correction boundary

Validated so far:

- Pattern mode can emit burst sequences, consume exact filter frame indices, and
  log continuous mic sample indices.
- Continuity-gated pattern matching prevents the previous 45 ms-style late-group
  jump.
- Demodulated envelope landmark mode produced zero pattern misses in the
  2026-05-28 smoke run and improved SNR substantially.
- Existing data replay shows absolute mic-vs-filter drift is mostly common-mode
  across the two speakers; group-relative residuals stayed within a few
  milliseconds.

Not yet validated:

- Live observe-only `relative_correction_proposed` logs from the Pi.
- Stability over a longer run than the 4 minute envelope smoke.
- Actuation from the relative residual into `set_rate_ppm`.
- Multi-speaker behavior beyond two BT speakers.
- UX surface and 24-hour soak.

Next validation step:

Run pattern mode with `--enable-relative-proposals`, without
`--enable-correction`. Success for that pass is stable
`relative_correction_proposed` records with small group-relative residuals and no
actuator writes. Only after that should pattern residuals be wired into
`set_rate_ppm`.
