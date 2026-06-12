# Workstream: Spatial Slice 1 per-speaker EQ balancing

- Branch: `maverick/syncsonic/spatial/slice-1-per-speaker-eq-balancing-measure-response-apply-pipewire-correction-filter-e31b022d`
- Lane: `spatial-audio-awareness`
- Updated: 2026-06-12 11:08 EDT

## Runtime Command Attempt

- Pi target `syncsonic@10.0.0.89` was reachable.
- `syncsonic.service` was active and advertising on `hci3`.
- Existing `runtime-latency.service` was inactive after a clean startup-gate timeout at 2026-06-12 11:00:19 EDT with `reason="no_connected_speaker_macs"`.
- The exact requested transient command using `--unit=runtime-latency` failed because `/etc/systemd/system/runtime-latency.service` already exists.
- A renamed transient unit preserving the requested flags, `runtime-latency-manual.service`, started but exited immediately because `runtime_latency_service.py` does not support `--enable-correction`.
- Local and remote parser inspection confirmed correction is the default behavior unless `--observe-only` is supplied.
- Started the valid correction-capable command as `runtime-latency-manual-valid.service` with `--max-speakers 2` and without `--enable-correction`.

## Verification Evidence

- `runtime-latency-manual-valid.service` started at 2026-06-12 11:08:47 EDT.
- Startup args logged `observe_only=false`, `slice4_observe=false`, and `max_speakers=2`.
- Initial discovery logs showed `active_macs=[]`, `connected_macs=[]`, and `filter_socket_macs=[]`.
- The unit entered startup gating with `reason="no_connected_speaker_macs"` and will not measure or apply corrections until a speaker connects.
- `bluetoothctl devices Connected` returned no connected devices.
- `PULSE_SERVER=unix:/run/syncsonic/pulse/native pactl list short sinks` showed only `virtual_out` and the local analog sink.

## Follow-up

- Connect at least one Bluetooth speaker, then inspect `journalctl -u runtime-latency-manual-valid.service -f --no-pager`.
- If the permanent unit should run this mode, update the installed service/drop-in instead of using a transient unit.
