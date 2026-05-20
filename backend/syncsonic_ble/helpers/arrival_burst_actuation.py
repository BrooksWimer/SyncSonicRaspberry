from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from measurement.probe_signals import (
    RUNTIME_BURST_DURATION_SEC,
    RUNTIME_BURST_FREQUENCIES_HZ,
    build_runtime_ultrasonic_burst,
    write_mono_s16_wav,
)
from syncsonic_ble.helpers.actuation import ActuationManager, get_actuation_manager
from syncsonic_ble.helpers.pipewire_transport import resolve_pipewire_output_name
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

DEFAULT_CONTEXT_WINDOW_MARGIN_MS = 150.0
DEFAULT_INTER_BURST_INTERVAL_SEC = 1.0


@dataclass(frozen=True)
class ArrivalDetectionWindow:
    start_sec: float
    end_sec: float
    expected_arrival_sec: float


@dataclass(frozen=True)
class ArrivalBurstTarget:
    mac: str
    sink: str
    delay_ms: float
    frequency_hz: float
    burst_duration_ms: float
    command: tuple[str, ...]
    scheduled_start_sec: float
    detection_window: ArrivalDetectionWindow


@dataclass(frozen=True)
class ArrivalBurstResult:
    target: ArrivalBurstTarget
    ok: bool
    returncode: int
    stderr: str = ""


ProgressCallback = Callable[[str, dict], None]
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
Clock = Callable[[], float]


def detection_window_for_delay(
    delay_ms: float,
    *,
    scheduled_start_sec: float = 0.0,
    margin_ms: float = DEFAULT_CONTEXT_WINDOW_MARGIN_MS,
) -> ArrivalDetectionWindow:
    """Return the context window centered on the ActuationManager delay."""
    expected = float(scheduled_start_sec) + max(0.0, float(delay_ms)) / 1000.0
    margin = max(0.0, float(margin_ms)) / 1000.0
    return ArrivalDetectionWindow(
        start_sec=max(0.0, expected - margin),
        end_sec=expected + margin,
        expected_arrival_sec=expected,
    )


def active_speaker_delays(manager: Optional[ActuationManager] = None) -> dict[str, float]:
    """Read active per-speaker delay targets from ActuationManager."""
    mgr = manager or get_actuation_manager()
    if hasattr(mgr, "get_delay_metadata"):
        metadata = mgr.get_delay_metadata(active_only=True)
        return {
            mac.upper(): float(values["delay_ms"])
            for mac, values in metadata.items()
            if isinstance(values, dict) and "delay_ms" in values
        }
    snapshot = mgr.get_status_snapshot()
    outputs: dict[str, float] = {}
    for mac, state in snapshot.items():
        if not isinstance(state, dict):
            continue
        if not state.get("active"):
            continue
        outputs[mac.upper()] = float(state.get(
            "delay_ms",
            state.get("delay_cmd_ms", state.get("delay_applied_ms", 100.0)),
        ))
    return outputs


def plan_arrival_bursts(
    mac_delays_ms: dict[str, float],
    *,
    frequencies_hz: Sequence[float] = RUNTIME_BURST_FREQUENCIES_HZ,
    interval_sec: float = DEFAULT_INTER_BURST_INTERVAL_SEC,
    sink_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> list[ArrivalBurstTarget]:
    """Plan one fixed-duration direct-to-sink arrival burst per speaker."""
    if not frequencies_hz:
        raise ValueError("frequencies_hz must not be empty")
    resolver = sink_resolver or resolve_pipewire_output_name
    targets: list[ArrivalBurstTarget] = []
    for idx, mac in enumerate(sorted(mac_delays_ms)):
        sink = resolver(mac)
        if not sink:
            log.warning("No PulseAudio sink found for runtime arrival burst target %s", mac)
            continue
        delay_ms = float(mac_delays_ms[mac])
        frequency_hz = float(frequencies_hz[idx % len(frequencies_hz)])
        scheduled_start_sec = idx * max(0.0, float(interval_sec))
        targets.append(
            ArrivalBurstTarget(
                mac=mac.upper(),
                sink=sink,
                delay_ms=delay_ms,
                frequency_hz=frequency_hz,
                burst_duration_ms=RUNTIME_BURST_DURATION_SEC * 1000.0,
                command=(),
                scheduled_start_sec=scheduled_start_sec,
                detection_window=detection_window_for_delay(
                    delay_ms,
                    scheduled_start_sec=scheduled_start_sec,
                ),
            )
        )
    return targets


class ArrivalBurstActuator:
    """Emit Slice 1 ultrasonic arrival bursts directly to speaker sinks."""

    def __init__(
        self,
        *,
        manager: Optional[ActuationManager] = None,
        run_command: RunCommand = subprocess.run,
        clock: Clock = time.monotonic,
    ) -> None:
        self._manager = manager or get_actuation_manager()
        self._run_command = run_command
        self._clock = clock

    def emit_once(
        self,
        *,
        macs: Optional[Iterable[str]] = None,
        on_event: Optional[ProgressCallback] = None,
    ) -> list[ArrivalBurstResult]:
        if shutil.which("paplay") is None:
            raise RuntimeError("paplay_not_available")

        delays = active_speaker_delays(self._manager)
        if macs is not None:
            wanted = {mac.upper() for mac in macs}
            delays = {mac: delay for mac, delay in delays.items() if mac in wanted}
        targets = plan_arrival_bursts(delays)

        results: list[ArrivalBurstResult] = []
        with tempfile.TemporaryDirectory(prefix="syncsonic-arrival-") as tmp:
            tmp_dir = Path(tmp)
            burst_paths: dict[float, Path] = {}
            started_at = self._clock()
            for target in targets:
                burst_path = self._burst_wav_path(tmp_dir, target.frequency_hz, burst_paths)
                command = ("paplay", f"--device={target.sink}", str(burst_path))
                target = ArrivalBurstTarget(
                    **{**asdict(target), "command": command, "detection_window": target.detection_window}
                )
                self._sleep_until(started_at + target.scheduled_start_sec)
                if on_event:
                    on_event("burst_start", _target_payload(target))
                completed = self._run_command(
                    list(command),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5.0,
                )
                result = ArrivalBurstResult(
                    target=target,
                    ok=completed.returncode == 0,
                    returncode=int(completed.returncode),
                    stderr=(completed.stderr or "").strip(),
                )
                results.append(result)
                if on_event:
                    on_event("burst_complete", _result_payload(result))
        return results

    def _burst_wav_path(self, tmp_dir: Path, frequency_hz: float, cache: dict[float, Path]) -> Path:
        cached = cache.get(frequency_hz)
        if cached is not None:
            return cached
        path = tmp_dir / f"syncsonic_arrival_{int(round(frequency_hz))}hz_100ms.wav"
        write_mono_s16_wav(path, build_runtime_ultrasonic_burst(frequency_hz=frequency_hz))
        cache[frequency_hz] = path
        return path

    def _sleep_until(self, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining > 0:
            time.sleep(remaining)


def _target_payload(target: ArrivalBurstTarget) -> dict:
    return {
        "mac": target.mac,
        "sink": target.sink,
        "delay_ms": target.delay_ms,
        "frequency_hz": target.frequency_hz,
        "burst_duration_ms": target.burst_duration_ms,
        "scheduled_start_sec": target.scheduled_start_sec,
        "detection_window": asdict(target.detection_window),
    }


def _result_payload(result: ArrivalBurstResult) -> dict:
    payload = _target_payload(result.target)
    payload.update({
        "ok": result.ok,
        "returncode": result.returncode,
        "stderr": result.stderr,
    })
    return payload
