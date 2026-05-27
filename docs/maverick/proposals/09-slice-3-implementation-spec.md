# Slice 3 implementation spec — closed-loop drift correction

_Operator-decided implementation template for the slice 3 Maverick workstream. Planners + Codex should template the implementation directly from this doc rather than re-deriving design from PROJECT_MEMORY (which has the rationale but not the exact code shape)._

## File constraints

- Modify ONLY `backend/measurement/runtime_latency_service.py` (extend in-place).
- Do NOT modify slice 2 code paths: `EnvelopeDetector`, `RingBuffer`, `ParecordCapture`, `_send_filter_command`, `discover_active_speakers`, `_measurement_loop` core. Extend at integration points.
- Do NOT create new files / directories / systemd units / CLI shims / IPC sockets.
- Do NOT modify `pyproject.toml`.

## Real class + helper names (verify by reading the file first)

- Existing service class: **`RuntimeSyncService`** (NOT `RuntimeLatencyService` — the file got renamed during slice 2 cleanup, the class did not).
- Existing per-speaker dataclass: `SpeakerTarget`.
- Existing helper: `_send_filter_command(socket_path: Path, payload: str)`.
- Existing detector: `EnvelopeDetector` (warmup, detect, _band_power_db).
- Existing measurement loop: `RuntimeSyncService._measurement_loop` and `_measure_once`.

## New class to add (in same file, after `EnvelopeDetector`)

```python
class DriftController:
    """Per-speaker drift correction state + decision logic."""

    def __init__(self, max_ppm: float = 50.0, smoothing_window: int = 5, gain: float = 0.1):
        # max_ppm: clamp bound per ROADMAP §4
        # smoothing_window: rolling-mean depth for drift estimation
        # gain: proportional gain on drift -> applied_ppm.
        # 0.1 is a conservative starting value; tune after Pi validation.
        self.max_ppm = max_ppm
        self.smoothing_window = smoothing_window
        self.gain = gain
        self.baselines: dict[str, float] = {}
        self.recent_samples: dict[str, list[float]] = {}
        self.consecutive_skips: dict[str, int] = {}
        self.paused: set[str] = set()

    def observe(
        self,
        mac: str,
        latency_ms: float,
        slider_ms: float,
        snr_db: float,
        stable_count: int,
    ) -> Optional[dict]:
        """Record a measurement; return a JSON-line-ready record dict or None for no-op."""
        codec_ms = latency_ms - slider_ms

        # Update rolling samples
        recent = self.recent_samples.setdefault(mac, [])
        recent.append(codec_ms)
        if len(recent) > self.smoothing_window:
            recent.pop(0)

        # First baseline once we have enough stable measurements
        if mac not in self.baselines and stable_count >= self.smoothing_window:
            self.baselines[mac] = sum(recent) / len(recent)
            return {
                "event": "correction_proposed",
                "mac": mac,
                "reason": "baseline_established",
                "baseline_codec_ms": self.baselines[mac],
            }

        # Already paused: no-op
        if mac in self.paused:
            return None

        # Confidence gating
        if stable_count < self.smoothing_window or snr_db < 10.0:
            self.consecutive_skips[mac] = self.consecutive_skips.get(mac, 0) + 1
            if self.consecutive_skips[mac] >= 3 and snr_db < 10.0:
                self.paused.add(mac)
                return {
                    "event": "controller_paused",
                    "mac": mac,
                    "reason": f"3+ low-confidence skips, snr={snr_db:.1f}dB",
                }
            return {
                "event": "correction_skipped",
                "mac": mac,
                "reason": "low_confidence",
                "snr_db": snr_db,
                "stable_count": stable_count,
            }

        # Compute correction
        self.consecutive_skips[mac] = 0
        if mac not in self.baselines:
            return None
        recent_mean = sum(recent) / len(recent)
        drift_ms = recent_mean - self.baselines[mac]
        applied_ppm = max(-self.max_ppm, min(self.max_ppm, drift_ms * self.gain))
        return {
            "event": "correction_applied",
            "mac": mac,
            "baseline_codec_ms": self.baselines[mac],
            "current_codec_ms": recent_mean,
            "drift_ppm_estimated": drift_ms * self.gain,
            "applied_ppm": applied_ppm,
        }
```

## Integration into `RuntimeSyncService`

**In `__init__`:**

```python
self.controller: Optional[DriftController] = (
    DriftController(max_ppm=args.max_ppm, smoothing_window=args.smoothing_window)
    if args.enable_correction else None
)
```

**In `_measure_once`:** after the existing arrival detection + `burst_arrival` `_emit` call, if a successful detection was produced and `self.controller is not None`:

```python
if self.controller is not None and snr_db is not None and stable_count is not None:
    record = self.controller.observe(
        target.mac, latency_ms, slider_target_delay_ms, snr_db, target.stable_count
    )
    if record is not None:
        _emit(**record)
        if record["event"] == "correction_applied":
            response = _send_filter_command(
                target.socket_path, f"set_rate_ppm {record['applied_ppm']:.3f}"
            )
            _emit(
                event="correction_applied_response",
                mac=target.mac,
                response=response,
                applied_ppm=record["applied_ppm"],
            )
```

(Field names like `snr_db`, `stable_count`, `latency_ms`, `slider_target_delay_ms` should match the names slice 2 already uses inside `_measure_once`. Verify against the actual file.)

## CLI flags (in `_build_parser`)

```python
parser.add_argument(
    "--enable-correction",
    action="store_true",
    help="Enable closed-loop drift correction (default: measure-only)",
)
parser.add_argument(
    "--max-ppm",
    type=float,
    default=50.0,
    help="Maximum |ppm| correction bound per ROADMAP §4",
)
parser.add_argument(
    "--smoothing-window",
    type=int,
    default=5,
    help="Rolling-mean window for drift estimation",
)
```

## Log events (extend slice 2 JSON-lines schema; emitted via existing `_emit` helper)

| event | payload fields |
|---|---|
| `correction_proposed` | mac, reason, baseline_codec_ms (when baseline first set) |
| `correction_applied` | mac, baseline_codec_ms, current_codec_ms, drift_ppm_estimated, applied_ppm |
| `correction_skipped` | mac, reason, snr_db, stable_count |
| `controller_paused` | mac, reason |
| `correction_applied_response` | mac, response (from `_send_filter_command`), applied_ppm |

## Verification

- `python3 -m py_compile backend/measurement/runtime_latency_service.py` passes.
- `python3 backend/measurement/runtime_latency_service.py --help` shows `--enable-correction`, `--max-ppm`, `--smoothing-window`.
- `python3 backend/measurement/runtime_latency_service.py` (no flags) behaves exactly as slice 2 (measure-only, no controller).
- `python3 backend/measurement/runtime_latency_service.py --enable-correction` creates the controller and the new event kinds appear in stdout JSON-lines.

## Out of scope

- UX surface (SpeakerConfigScreen.tsx) — slice 4+.
- BLE in-band on/off toggle — slice 4+.
- Frequency rotation across speakers — slice 4+.
- 24-hour soak validation — slice 4+.
- Audio-clock alignment refactor — deferred; revisit if wall-clock jitter becomes the bottleneck.
- Third-speaker scaling investigation — deferred.

## Pi validation (operator's responsibility post-merge)

30-minute music session with `--enable-correction`, both speakers playing, no operator intervention. Journal log should show: corrections within ±50 ppm, per-speaker latency staying within ±10 ms of starting baseline, zero `controller_paused` events on a healthy system.
