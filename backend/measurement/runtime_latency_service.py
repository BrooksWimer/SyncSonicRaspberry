"""Open-loop ultrasonic runtime latency measurement service.

Slice 2 deliberately measures only: filter-resident ultrasonic burst
emission, USB mic capture, envelope detection, and JSON-lines journal
records. Pattern mode feeds valid measurements into the Slice 5 delay-step actuator.

Invocation (manual CLI; runs as the syncsonic user so it can access
the filter sockets and the service's PipeWire/Pulse runtime):

    sudo systemd-run --unit=runtime-latency \\
        --uid=syncsonic --gid=syncsonic \\
        --working-directory=/home/syncsonic/SyncSonicPi \\
        --setenv=PULSE_SERVER=unix:/run/syncsonic/pulse/native \\
        --setenv=RESERVED_HCI=hci3 \\
        --setenv=RESERVED_ADAPTER_MAC=2C:CF:67:CE:57:91 \\
        --setenv=PYTHONUNBUFFERED=1 \\
        python3 /home/syncsonic/SyncSonicPi/backend/measurement/runtime_latency_service.py \\
        --max-speakers 2

The four ``--setenv`` lines are mandatory:
- ``PULSE_SERVER`` points at the syncsonic-owned PulseAudio runtime
  so ``parecord`` can read the USB mic source. Without it the service
  fails to start capture because the default PulseAudio path is empty.
- ``RESERVED_HCI`` and ``RESERVED_ADAPTER_MAC`` are required by
  ``syncsonic_ble.helpers.adapter_helpers`` to pick the BlueZ HCI
  adapter that owns the speaker connections. Values mirror what
  ``syncsonic.service`` reads from ``/etc/default/syncsonic`` -- query
  that file on the Pi for the canonical values.
- ``PYTHONUNBUFFERED=1`` ensures JSON-line records flush immediately
  to journald rather than buffering until the buffer fills.

Stop with ``sudo systemctl stop runtime-latency`` (sends SIGTERM, the
service handles via the existing ``stop_event``).

Inspect log with ``sudo journalctl -u runtime-latency -f``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import signal
import socket
import subprocess
import sys
# Slice 2 lives at backend/measurement/runtime_latency_service.py — add
# backend/ to sys.path so imports like `from syncsonic_ble.helpers...` resolve.
_BACKEND_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Iterable, Optional

import numpy as np

from measurement.service_env import slice4_observe_from_env
from measurement.slice4_observer import DEFAULT_OBSERVATION_PATH, ObservationWriter
from measurement.slice5_actuator import (
    BURST_AMP_X1000,
    ActuationResult,
    SpeakerActuator,
    register_ble_stop_callback,
)

try:
    import dbus
except ImportError:  # pragma: no cover - desktop tests can exercise pure detector math without DBus.
    dbus = None  # type: ignore[assignment]

SOCKET_DIR = Path(os.environ.get("SYNCSONIC_FILTER_SOCKET_DIR", "/tmp/syncsonic-engine"))
FILTER_SOCKET_GLOB = "syncsonic-delay-*.sock"
BLUEZ_SERVICE_NAME = "org.bluez"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
SAMPLE_RATE = 48_000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1
DEFAULT_MIC_SOURCE_PREFIX = "alsa_input.usb-Jieli"
DEFAULT_CADENCE_SEC = 15.0
DEFAULT_WARMUP_SEC = 5.0
DEFAULT_FREQ_HZ = 18_500.0
DEFAULT_DURATION_MS = 100
DEFAULT_AMPLITUDE = 0.95
DEFAULT_BT_CODEC_LATENCY_MS = 370.0
INITIAL_WINDOW_MARGIN_MS = 250.0
STABLE_WINDOW_MARGIN_MS = 100.0
STABLE_MEASUREMENT_COUNT = 5
WINDOW_MS = 50.0
HOP_MS = 25.0
ONSET_WINDOW_MS = 10.0
ONSET_HOP_MS = 2.5
PATTERN_TOLERANCE_MS = 35.0
PATTERN_CLOCK_TOLERANCE_MS = 20.0
PATTERN_MIN_SNR_DB = 9.0
PATTERN_LANDMARK = "envelope"
ENVELOPE_MIX_LOW_PASS_MS = 1.5
ENVELOPE_SMOOTH_MS = 2.0
ENVELOPE_REFRACTORY_MS = 25.0
ENVELOPE_EDGE_PRE_MS = 4.0
ENVELOPE_EDGE_POST_MS = 8.0
SLICE4_HISTORY_LIMIT = 5
MIN_SNR_DB = 12.0
SOCKET_TIMEOUT_SEC = 1.5
CLOCK_PRIOR_RESET_CYCLES = 3


def _emit(event: str, **fields: Any) -> None:
    record = {
        "event": event,
        "component": "runtime_sync",
        "ts_unix": time.time(),
        "monotonic": time.monotonic(),
        **fields,
    }
    print(json.dumps(record, sort_keys=True), flush=True)


def _mac_from_socket_filename(name: str) -> Optional[str]:
    prefix = "syncsonic-delay-"
    suffix = ".sock"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    body = name[len(prefix) : -len(suffix)]
    parts = body.split("_")
    if len(parts) != 6 or any(len(p) != 2 for p in parts):
        return None
    return ":".join(parts).upper()


def _send_filter_command(socket_path: Path, payload: str) -> Optional[dict[str, Any]]:
    """Send one JSON-line filter command without using transport singletons."""
    if not socket_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(SOCKET_TIMEOUT_SEC)
            sock.connect(str(socket_path))
            sock.sendall((payload + "\n").encode("ascii"))
            buf = b""
            while b"\n" not in buf and len(buf) < 4096:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout) as exc:
        _emit("filter_command_failed", socket_path=str(socket_path), payload=payload, error=repr(exc))
        return None
    try:
        line = buf.decode("ascii", errors="replace").strip().split("\n")[0]
        return json.loads(line)
    except (IndexError, json.JSONDecodeError, ValueError) as exc:
        _emit("filter_command_bad_response", socket_path=str(socket_path), payload=payload, error=repr(exc))
        return None


def _scan_filter_sockets() -> dict[str, Path]:
    sockets: dict[str, Path] = {}
    if not SOCKET_DIR.exists():
        return sockets
    for path in SOCKET_DIR.glob(FILTER_SOCKET_GLOB):
        mac = _mac_from_socket_filename(path.name)
        if mac:
            sockets[mac] = path
    return sockets


def _connected_speaker_macs() -> set[str]:
    """Return connected BlueZ devices, degrading to empty on any probe failure."""
    if dbus is None:
        raise RuntimeError("dbus module unavailable")
    from syncsonic_ble.helpers.adapter_helpers import connected_devices_on_adapter

    bus = dbus.SystemBus()
    om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
    connected: set[str] = set()
    for path, ifaces in om.GetManagedObjects().items():
        if ADAPTER_INTERFACE not in ifaces:
            continue
        connected.update(mac.upper() for mac in connected_devices_on_adapter(bus, str(path)))
    return connected


def discover_active_speakers(limit: int = 2) -> list["SpeakerTarget"]:
    try:
        sockets = _scan_filter_sockets()
        connected = _connected_speaker_macs()
        active = sorted(mac for mac in sockets if mac in connected)
        targets = [SpeakerTarget(mac=mac, socket_path=sockets[mac]) for mac in active[:limit]]
        _emit(
            "device_discovery",
            filter_socket_macs=sorted(sockets),
            connected_macs=sorted(connected),
            active_macs=[target.mac for target in targets],
            limit=limit,
        )
        return targets
    except Exception as exc:  # noqa: BLE001 - missing adapter must not crash service
        _emit("device_discovery_failed", error=repr(exc))
        return []


@dataclass
class SampleChunk:
    start_time: float
    end_time: float
    start_index: int
    end_index: int
    samples: np.ndarray


@dataclass
class SampleWindow:
    samples: np.ndarray
    start_time: float
    start_index: int


class RingBuffer:
    """Timestamped and sample-indexed mic ring fed by long-running parecord."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, capacity_sec: float = 20.0) -> None:
        self.sample_rate = sample_rate
        self.capacity_samples = int(sample_rate * capacity_sec)
        self._chunks: Deque[SampleChunk] = deque()
        self._samples = 0
        self._next_sample_index = 0
        self._lock = asyncio.Lock()

    async def append(self, pcm: bytes, end_time: float) -> None:
        if not pcm:
            return
        usable = len(pcm) - (len(pcm) % SAMPLE_WIDTH_BYTES)
        if usable <= 0:
            return
        samples = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32) / 32768.0
        duration = len(samples) / self.sample_rate
        async with self._lock:
            start_index = self._next_sample_index
            end_index = start_index + len(samples)
            self._next_sample_index = end_index
            chunk = SampleChunk(
                start_time=end_time - duration,
                end_time=end_time,
                start_index=start_index,
                end_index=end_index,
                samples=samples,
            )
            self._chunks.append(chunk)
            self._samples += len(samples)
            while self._samples > self.capacity_samples and self._chunks:
                removed = self._chunks.popleft()
                self._samples -= len(removed.samples)

    async def read_window(self, start_time: float, end_time: float) -> tuple[np.ndarray, float]:
        window = await self.read_window_with_index(start_time, end_time)
        return window.samples, window.start_time

    async def read_window_with_index(self, start_time: float, end_time: float) -> SampleWindow:
        async with self._lock:
            pieces: list[np.ndarray] = []
            first_time: Optional[float] = None
            first_index: Optional[int] = None
            for chunk in self._chunks:
                if chunk.end_time <= start_time:
                    continue
                if chunk.start_time >= end_time:
                    break
                start_idx = max(0, int(math.floor((start_time - chunk.start_time) * self.sample_rate)))
                end_idx = min(len(chunk.samples), int(math.ceil((end_time - chunk.start_time) * self.sample_rate)))
                if end_idx <= start_idx:
                    continue
                if first_time is None:
                    first_time = chunk.start_time + (start_idx / self.sample_rate)
                    first_index = chunk.start_index + start_idx
                pieces.append(chunk.samples[start_idx:end_idx])
        if not pieces:
            return SampleWindow(np.zeros(0, dtype=np.float32), start_time, 0)
        return SampleWindow(
            np.concatenate(pieces),
            first_time if first_time is not None else start_time,
            first_index if first_index is not None else 0,
        )


class ParecordCapture:
    def __init__(self, ring: RingBuffer, source: Optional[str], source_prefix: str) -> None:
        self.ring = ring
        self.source = source
        self.source_prefix = source_prefix
        self.process: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        source = self.source or await asyncio.to_thread(_find_mic_source, self.source_prefix)
        if not source:
            raise RuntimeError(f"no PulseAudio/PipeWire source matching {self.source_prefix!r}")
        cmd = [
            "parecord",
            "--file-format=raw",
            "--format=s16le",
            f"--rate={SAMPLE_RATE}",
            f"--channels={CHANNELS}",
            f"--device={source}",
        ]
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._task = asyncio.create_task(self._reader())
        _emit("mic_capture_started", source=source, pid=self.process.pid, command=cmd)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.process and self.process.returncode is None:
            self.process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
            if self.process.returncode is None:
                self.process.kill()
        _emit("mic_capture_stopped")

    async def _reader(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        bytes_per_read = int(SAMPLE_RATE * SAMPLE_WIDTH_BYTES * 0.05)
        while True:
            chunk = await self.process.stdout.read(bytes_per_read)
            if not chunk:
                break
            await self.ring.append(chunk, time.monotonic())
        stderr = b""
        if self.process.stderr is not None:
            with contextlib.suppress(Exception):
                stderr = await self.process.stderr.read()
        _emit(
            "mic_capture_exited",
            returncode=self.process.returncode,
            stderr=stderr.decode("utf-8", errors="replace")[-1000:],
        )


def _find_mic_source(prefix: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and prefix in parts[1]:
            return parts[1]
    return None


@dataclass
class SpeakerTarget:
    mac: str
    socket_path: Path
    stable_count: int = 0
    last_latency_ms: Optional[float] = None
    latency_history_ms: list[float] = field(default_factory=list)
    sample_clock_baseline_samples: Optional[float] = None
    last_sample_clock_delta_samples: Optional[float] = None
    pattern_clock_reject_count: int = 0
    clock_prior_reset_remaining: int = 0


@dataclass
class RuntimeSyncState:
    measuring: bool = False
    targets: list[SpeakerTarget] = field(default_factory=list)
    cycles: int = 0


class EnvelopeDetector:
    def __init__(self, ring: RingBuffer) -> None:
        self.ring = ring
        self.noise_floor_db = -90.0

    async def warmup(self, duration_sec: float) -> None:
        await asyncio.sleep(duration_sec)
        now = time.monotonic()
        samples, _base = await self.ring.read_window(now - duration_sec, now)
        self.noise_floor_db = self._band_power_db(samples) if len(samples) else -90.0
        _emit("detector_warmup", duration_sec=duration_sec, noise_floor_db=self.noise_floor_db)

    async def detect(
        self,
        start_time: float,
        end_time: float,
        mode: str = "peak",
    ) -> Optional[dict[str, Any]]:
        window = await self.ring.read_window_with_index(start_time, end_time)
        if mode == "onset":
            return self.detect_onset_in_samples(
                window.samples,
                window.start_time,
                window.start_index,
                self.noise_floor_db,
            )
        return self.detect_peak_in_samples(
            window.samples,
            window.start_time,
            window.start_index,
            self.noise_floor_db,
        )

    async def detect_pattern(
        self,
        start_time: float,
        end_time: float,
        emit_frame_indices: list[int],
        tolerance_ms: float = PATTERN_TOLERANCE_MS,
        expected_delta_samples: Optional[float] = None,
        clock_tolerance_ms: float = PATTERN_CLOCK_TOLERANCE_MS,
        min_snr_db: float = PATTERN_MIN_SNR_DB,
        landmark: str = PATTERN_LANDMARK,
        carrier_hz: float = DEFAULT_FREQ_HZ,
    ) -> Optional[dict[str, Any]]:
        analysis = await self.analyze_pattern(
            start_time,
            end_time,
            emit_frame_indices,
            tolerance_ms=tolerance_ms,
            expected_delta_samples=expected_delta_samples,
            clock_tolerance_ms=clock_tolerance_ms,
            min_snr_db=min_snr_db,
            landmark=landmark,
            carrier_hz=carrier_hz,
        )
        return analysis.get("selected")

    async def analyze_pattern(
        self,
        start_time: float,
        end_time: float,
        emit_frame_indices: list[int],
        tolerance_ms: float = PATTERN_TOLERANCE_MS,
        expected_delta_samples: Optional[float] = None,
        clock_tolerance_ms: float = PATTERN_CLOCK_TOLERANCE_MS,
        min_snr_db: float = PATTERN_MIN_SNR_DB,
        landmark: str = PATTERN_LANDMARK,
        carrier_hz: float = DEFAULT_FREQ_HZ,
    ) -> dict[str, Any]:
        window = await self.ring.read_window_with_index(start_time, end_time)
        return self.analyze_pattern_in_samples(
            window.samples,
            window.start_time,
            window.start_index,
            self.noise_floor_db,
            emit_frame_indices,
            tolerance_ms=tolerance_ms,
            expected_delta_samples=expected_delta_samples,
            clock_tolerance_ms=clock_tolerance_ms,
            min_snr_db=min_snr_db,
            landmark=landmark,
            carrier_hz=carrier_hz,
        )

    @classmethod
    def detect_peak_in_samples(
        cls,
        samples: np.ndarray,
        base_time: float,
        base_sample_index: int,
        noise_floor_db: float,
    ) -> Optional[dict[str, Any]]:
        window = int(SAMPLE_RATE * WINDOW_MS / 1000.0)
        hop = int(SAMPLE_RATE * HOP_MS / 1000.0)
        if len(samples) < window:
            return None
        best_db = -200.0
        best_idx = 0
        for idx in range(0, len(samples) - window + 1, hop):
            power = cls._band_power_db(samples[idx : idx + window])
            if power > best_db:
                best_db = power
                best_idx = idx
        snr_db = best_db - noise_floor_db
        if snr_db < MIN_SNR_DB:
            return None
        arrival_offset = best_idx + (window / 2)
        arrival_time = base_time + (arrival_offset / SAMPLE_RATE)
        arrival_sample_index = base_sample_index + int(round(arrival_offset))
        return {
            "arrival_monotonic": arrival_time,
            "arrival_sample_index": arrival_sample_index,
            "peak_power_db": best_db,
            "noise_floor_db": noise_floor_db,
            "snr_db": snr_db,
            "detector_mode": "peak",
        }

    @classmethod
    def detect_onset_in_samples(
        cls,
        samples: np.ndarray,
        base_time: float,
        base_sample_index: int,
        noise_floor_db: float,
    ) -> Optional[dict[str, Any]]:
        candidates = cls._onset_candidates(samples, base_sample_index, noise_floor_db)
        if not candidates:
            return None
        first = candidates[0]
        offset = first["sample_index"] - base_sample_index
        return {
            "arrival_monotonic": base_time + (offset / SAMPLE_RATE),
            "arrival_sample_index": first["sample_index"],
            "peak_power_db": first["power_db"],
            "noise_floor_db": noise_floor_db,
            "snr_db": first["snr_db"],
            "detector_mode": "onset",
            "candidate_count": len(candidates),
        }

    @classmethod
    def detect_pattern_in_samples(
        cls,
        samples: np.ndarray,
        base_time: float,
        base_sample_index: int,
        noise_floor_db: float,
        emit_frame_indices: list[int],
        tolerance_ms: float = PATTERN_TOLERANCE_MS,
        expected_delta_samples: Optional[float] = None,
        clock_tolerance_ms: float = PATTERN_CLOCK_TOLERANCE_MS,
        min_snr_db: float = PATTERN_MIN_SNR_DB,
        landmark: str = PATTERN_LANDMARK,
        carrier_hz: float = DEFAULT_FREQ_HZ,
    ) -> Optional[dict[str, Any]]:
        analysis = cls.analyze_pattern_in_samples(
            samples,
            base_time,
            base_sample_index,
            noise_floor_db,
            emit_frame_indices,
            tolerance_ms=tolerance_ms,
            expected_delta_samples=expected_delta_samples,
            clock_tolerance_ms=clock_tolerance_ms,
            min_snr_db=min_snr_db,
            landmark=landmark,
            carrier_hz=carrier_hz,
        )
        return analysis.get("selected")

    @classmethod
    def analyze_pattern_in_samples(
        cls,
        samples: np.ndarray,
        base_time: float,
        base_sample_index: int,
        noise_floor_db: float,
        emit_frame_indices: list[int],
        tolerance_ms: float = PATTERN_TOLERANCE_MS,
        expected_delta_samples: Optional[float] = None,
        clock_tolerance_ms: float = PATTERN_CLOCK_TOLERANCE_MS,
        min_snr_db: float = PATTERN_MIN_SNR_DB,
        landmark: str = PATTERN_LANDMARK,
        carrier_hz: float = DEFAULT_FREQ_HZ,
    ) -> dict[str, Any]:
        analysis: dict[str, Any] = {
            "candidate_count": 0,
            "pattern_match_count": 0,
            "pattern_rejected_by_clock_count": 0,
            "pattern_clock_prior_delta_samples": expected_delta_samples,
            "pattern_clock_prior_tolerance_ms": clock_tolerance_ms if expected_delta_samples is not None else None,
            "pattern_min_snr_db": min_snr_db,
            "pattern_landmark": landmark,
            "pattern_carrier_hz": carrier_hz,
        }
        if not emit_frame_indices:
            analysis["reject_reason"] = "missing_emit_frame_indices"
            return analysis
        if landmark == "envelope":
            candidates, landmark_scan = cls._demodulated_envelope_scan(
                samples,
                base_sample_index,
                carrier_hz=carrier_hz,
                min_snr_db=min_snr_db,
            )
        else:
            candidates, landmark_scan = cls._onset_scan(
                samples,
                base_sample_index,
                noise_floor_db,
                min_snr_db=min_snr_db,
            )
        analysis.update(landmark_scan)
        analysis["candidate_count"] = len(candidates)
        if not candidates:
            analysis["reject_reason"] = "no_onset_candidates"
            return analysis
        emit_offsets = [frame - emit_frame_indices[0] for frame in emit_frame_indices]
        tolerance_samples = int(round(tolerance_ms * SAMPLE_RATE / 1000.0))
        matches: list[dict[str, Any]] = []
        for start in candidates:
            matched: list[dict[str, float]] = []
            total_abs_error = 0.0
            for emit_frame, expected_offset in zip(emit_frame_indices, emit_offsets):
                expected_sample = start["sample_index"] + expected_offset
                nearest = min(
                    candidates,
                    key=lambda candidate: abs(candidate["sample_index"] - expected_sample),
                )
                error_samples = nearest["sample_index"] - expected_sample
                if abs(error_samples) > tolerance_samples:
                    break
                matched.append(
                    {
                        "sample_index": nearest["sample_index"],
                        "expected_sample_index": expected_sample,
                        "emit_frame_index": emit_frame,
                        "error_samples": error_samples,
                        "power_db": nearest["power_db"],
                        "snr_db": nearest["snr_db"],
                        "landmark_offset_ms": nearest.get("landmark_offset_ms"),
                    }
                )
                total_abs_error += abs(error_samples)
            if len(matched) != len(emit_offsets):
                continue
            clock_deltas = [
                float(match["sample_index"] - match["emit_frame_index"]) for match in matched
            ]
            mean_abs_error_samples = total_abs_error / max(1, len(matched))
            clock_delta_samples = float(sum(clock_deltas) / len(clock_deltas))
            match = {
                "first": start,
                "matched": matched,
                "mean_abs_error_samples": mean_abs_error_samples,
                "max_abs_error_samples": max(abs(match["error_samples"]) for match in matched),
                "clock_delta_samples": clock_delta_samples,
                "clock_delta_spread_samples": max(clock_deltas) - min(clock_deltas),
            }
            if expected_delta_samples is not None:
                match["clock_prior_error_samples"] = clock_delta_samples - expected_delta_samples
            matches.append(match)
        analysis["pattern_match_count"] = len(matches)
        if not matches:
            analysis["reject_reason"] = "pattern_not_matched"
            return analysis
        best_unprioritized = min(matches, key=lambda match: match["mean_abs_error_samples"])
        analysis.update(cls._pattern_match_debug(best_unprioritized, prefix="best_unprioritized"))
        if expected_delta_samples is not None:
            clock_tolerance_samples = clock_tolerance_ms * SAMPLE_RATE / 1000.0
            viable = [
                match
                for match in matches
                if abs(match["clock_prior_error_samples"]) <= clock_tolerance_samples
            ]
            analysis["pattern_rejected_by_clock_count"] = len(matches) - len(viable)
            if not viable:
                analysis["reject_reason"] = "clock_prior_mismatch"
                return analysis
            best = min(
                viable,
                key=lambda match: (
                    abs(match["clock_prior_error_samples"]),
                    match["mean_abs_error_samples"],
                ),
            )
            selection_reason = "clock_prior"
        else:
            best = best_unprioritized
            selection_reason = "best_spacing"
        first = best["first"]
        offset = first["sample_index"] - base_sample_index
        snrs = [match["snr_db"] for match in best["matched"]]
        powers = [match["power_db"] for match in best["matched"]]
        clock_delta_samples = best["clock_delta_samples"]
        clock_anchor_sample_index = emit_frame_indices[0] + clock_delta_samples
        selected = {
            "arrival_monotonic": base_time + (offset / SAMPLE_RATE),
            "arrival_sample_index": first["sample_index"],
            "sample_clock_anchor_sample_index": clock_anchor_sample_index,
            "sample_clock_anchor_monotonic": base_time
            + ((clock_anchor_sample_index - base_sample_index) / SAMPLE_RATE),
            "clock_delta_samples": clock_delta_samples,
            "peak_power_db": max(powers),
            "noise_floor_db": analysis.get("envelope_noise_floor_db", noise_floor_db),
            "snr_db": min(snrs),
            "detector_mode": "pattern",
            "candidate_count": len(candidates),
            "pattern_min_snr_db": min_snr_db,
            "pattern_landmark": landmark,
            "pattern_carrier_hz": carrier_hz,
            "matched_arrival_sample_indices": [int(match["sample_index"]) for match in best["matched"]],
            "matched_error_ms": [match["error_samples"] * 1000.0 / SAMPLE_RATE for match in best["matched"]],
            "matched_landmark_offset_ms": [
                match.get("landmark_offset_ms") for match in best["matched"]
            ],
            "pattern_mean_abs_error_ms": best["mean_abs_error_samples"] * 1000.0 / SAMPLE_RATE,
            "pattern_max_abs_error_ms": best["max_abs_error_samples"] * 1000.0 / SAMPLE_RATE,
            "pattern_clock_delta_spread_ms": best["clock_delta_spread_samples"] * 1000.0 / SAMPLE_RATE,
            "pattern_selection_reason": selection_reason,
            "pattern_match_count": len(matches),
            "pattern_rejected_by_clock_count": analysis["pattern_rejected_by_clock_count"],
        }
        for key in (
            "envelope_noise_floor_db",
            "envelope_threshold_db",
            "envelope_peak_db",
            "envelope_peak_snr_db",
            "envelope_mix_low_pass_ms",
            "envelope_smooth_ms",
        ):
            if key in analysis:
                selected[key] = analysis[key]
        if expected_delta_samples is not None:
            selected["pattern_clock_prior_delta_samples"] = expected_delta_samples
            selected["pattern_clock_prior_error_ms"] = (
                best["clock_prior_error_samples"] * 1000.0 / SAMPLE_RATE
            )
            selected["pattern_clock_prior_tolerance_ms"] = clock_tolerance_ms
        analysis["selected"] = selected
        return analysis

    @staticmethod
    def _pattern_match_debug(match: dict[str, Any], prefix: str) -> dict[str, Any]:
        return {
            f"{prefix}_pattern_mean_abs_error_ms": match["mean_abs_error_samples"] * 1000.0 / SAMPLE_RATE,
            f"{prefix}_pattern_max_abs_error_ms": match["max_abs_error_samples"] * 1000.0 / SAMPLE_RATE,
            f"{prefix}_sample_clock_delta_ms": match["clock_delta_samples"] * 1000.0 / SAMPLE_RATE,
            f"{prefix}_pattern_clock_delta_spread_ms": match["clock_delta_spread_samples"] * 1000.0 / SAMPLE_RATE,
            f"{prefix}_pattern_clock_prior_error_ms": (
                match.get("clock_prior_error_samples", 0.0) * 1000.0 / SAMPLE_RATE
                if "clock_prior_error_samples" in match
                else None
            ),
        }

    @classmethod
    def _onset_candidates(
        cls,
        samples: np.ndarray,
        base_sample_index: int,
        noise_floor_db: float,
        min_snr_db: float = MIN_SNR_DB,
    ) -> list[dict[str, Any]]:
        candidates, _scan = cls._onset_scan(
            samples,
            base_sample_index,
            noise_floor_db,
            min_snr_db=min_snr_db,
        )
        return candidates

    @classmethod
    def _onset_scan(
        cls,
        samples: np.ndarray,
        base_sample_index: int,
        noise_floor_db: float,
        min_snr_db: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        window = max(1, int(SAMPLE_RATE * ONSET_WINDOW_MS / 1000.0))
        hop = max(1, int(SAMPLE_RATE * ONSET_HOP_MS / 1000.0))
        threshold = noise_floor_db + min_snr_db
        scan: dict[str, Any] = {
            "onset_threshold_db": threshold,
            "onset_min_snr_db": min_snr_db,
            "onset_window_ms": ONSET_WINDOW_MS,
            "onset_hop_ms": ONSET_HOP_MS,
            "onset_bin_count": 0,
            "onset_max_power_db": None,
            "onset_max_snr_db": None,
        }
        if len(samples) < window:
            return [], scan
        bins: list[tuple[int, float]] = []
        for idx in range(0, len(samples) - window + 1, hop):
            power = cls._band_power_db(samples[idx : idx + window])
            bins.append((idx, power))
        scan["onset_bin_count"] = len(bins)
        if bins:
            max_power = max(power for _idx, power in bins)
            scan["onset_max_power_db"] = max_power
            scan["onset_max_snr_db"] = max_power - noise_floor_db
        candidates: list[dict[str, Any]] = []
        previous_above = False
        refractory = max(window, int(0.5 * window))
        last_sample_index = -refractory
        for idx, power in bins:
            above = power >= threshold
            if above and not previous_above:
                sample_index = base_sample_index + idx
                if sample_index - last_sample_index >= refractory:
                    candidates.append(
                        {
                            "sample_index": sample_index,
                            "power_db": power,
                            "snr_db": power - noise_floor_db,
                        }
                    )
                    last_sample_index = sample_index
            previous_above = above
        return candidates, scan

    @classmethod
    def _demodulated_envelope_scan(
        cls,
        samples: np.ndarray,
        base_sample_index: int,
        carrier_hz: float,
        min_snr_db: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        envelope = cls._demodulated_envelope(samples, base_sample_index, carrier_hz)
        threshold_floor = 1e-9
        envelope_db = 20.0 * np.log10(np.maximum(envelope, threshold_floor))
        scan: dict[str, Any] = {
            "envelope_noise_floor_db": None,
            "envelope_threshold_db": None,
            "envelope_peak_db": None,
            "envelope_peak_snr_db": None,
            "envelope_mix_low_pass_ms": ENVELOPE_MIX_LOW_PASS_MS,
            "envelope_smooth_ms": ENVELOPE_SMOOTH_MS,
            "envelope_refractory_ms": ENVELOPE_REFRACTORY_MS,
            "envelope_edge_pre_ms": ENVELOPE_EDGE_PRE_MS,
            "envelope_edge_post_ms": ENVELOPE_EDGE_POST_MS,
        }
        if len(envelope_db) == 0:
            return [], scan
        noise_floor_db = float(np.percentile(envelope_db, 35.0))
        peak_db = float(np.max(envelope_db))
        threshold_db = noise_floor_db + min_snr_db
        scan.update(
            {
                "envelope_noise_floor_db": noise_floor_db,
                "envelope_threshold_db": threshold_db,
                "envelope_peak_db": peak_db,
                "envelope_peak_snr_db": peak_db - noise_floor_db,
            }
        )
        if peak_db < threshold_db:
            return [], scan

        refractory_samples = max(1, int(round(ENVELOPE_REFRACTORY_MS * SAMPLE_RATE / 1000.0)))
        pre_samples = max(1, int(round(ENVELOPE_EDGE_PRE_MS * SAMPLE_RATE / 1000.0)))
        post_samples = max(1, int(round(ENVELOPE_EDGE_POST_MS * SAMPLE_RATE / 1000.0)))
        candidates: list[dict[str, Any]] = []
        above = envelope_db >= threshold_db
        previous_above = False
        last_sample_index = -refractory_samples
        for idx, is_above in enumerate(above):
            if bool(is_above) and not previous_above:
                sample_index = base_sample_index + idx
                if sample_index - last_sample_index >= refractory_samples:
                    edge_start = max(0, idx - pre_samples)
                    edge_end = min(len(envelope), idx + post_samples + 1)
                    edge = envelope[edge_start:edge_end]
                    if len(edge) >= 2:
                        slope = np.diff(edge)
                        slope_idx = int(np.argmax(slope))
                        landmark_offset = edge_start + slope_idx + 1
                    else:
                        landmark_offset = idx
                    peak_end = min(len(envelope), idx + refractory_samples)
                    local_peak_db = float(np.max(envelope_db[idx:peak_end])) if peak_end > idx else float(envelope_db[idx])
                    landmark_sample_index = base_sample_index + landmark_offset
                    candidates.append(
                        {
                            "sample_index": landmark_sample_index,
                            "threshold_sample_index": sample_index,
                            "power_db": local_peak_db,
                            "snr_db": local_peak_db - noise_floor_db,
                            "landmark_offset_ms": (landmark_offset - idx) * 1000.0 / SAMPLE_RATE,
                        }
                    )
                    last_sample_index = landmark_sample_index
            previous_above = bool(is_above)
        return candidates, scan

    @staticmethod
    def _demodulated_envelope(
        samples: np.ndarray,
        base_sample_index: int,
        carrier_hz: float,
    ) -> np.ndarray:
        if len(samples) == 0:
            return np.zeros(0, dtype=np.float64)
        sample_indices = base_sample_index + np.arange(len(samples), dtype=np.float64)
        phase = -2.0 * np.pi * carrier_hz * sample_indices / SAMPLE_RATE
        mixed = samples.astype(np.float64) * np.exp(1j * phase)
        mix_window = max(1, int(round(ENVELOPE_MIX_LOW_PASS_MS * SAMPLE_RATE / 1000.0)))
        mixed = np.convolve(mixed, np.ones(mix_window, dtype=np.float64) / mix_window, mode="same")
        envelope = 2.0 * np.abs(mixed)
        smooth_window = max(1, int(round(ENVELOPE_SMOOTH_MS * SAMPLE_RATE / 1000.0)))
        if smooth_window > 1:
            envelope = np.convolve(envelope, np.ones(smooth_window, dtype=np.float64) / smooth_window, mode="same")
        return envelope

    @staticmethod
    def _band_power_db(samples: np.ndarray) -> float:
        if len(samples) == 0:
            return -200.0
        windowed = samples * np.hanning(len(samples))
        spectrum = np.fft.rfft(windowed)
        freqs = np.fft.rfftfreq(len(samples), d=1.0 / SAMPLE_RATE)
        band = (freqs >= 17_500.0) & (freqs <= 20_000.0)
        if not np.any(band):
            return -200.0
        power = float(np.mean(np.abs(spectrum[band]) ** 2))
        return 10.0 * math.log10(max(power, 1e-20))



def _current_emit_entry(entries: list[Any]) -> Optional[dict[str, Any]]:
    parsed = [entry for entry in entries if isinstance(entry, dict) and "frame_index" in entry]
    if not parsed:
        return None
    return max(parsed, key=lambda entry: int(entry.get("frame_index") or 0))


def _sample_clock_fields(
    target: SpeakerTarget,
    detection: dict[str, Any],
    entries: list[Any],
) -> dict[str, Any]:
    entry = _current_emit_entry(entries)
    if entry is None or "arrival_sample_index" not in detection:
        return {}
    emit_frame_index = int(entry["frame_index"])
    arrival_sample_index = int(detection["arrival_sample_index"])
    delta_samples = float(detection.get("clock_delta_samples", arrival_sample_index - emit_frame_index))
    if target.sample_clock_baseline_samples is None:
        target.sample_clock_baseline_samples = float(delta_samples)
    drift_samples = float(delta_samples) - target.sample_clock_baseline_samples
    target.last_sample_clock_delta_samples = float(delta_samples)
    return {
        "emit_frame_index": emit_frame_index,
        "arrival_sample_index": arrival_sample_index,
        "sample_clock_delta_samples": delta_samples,
        "sample_clock_delta_ms": delta_samples * 1000.0 / SAMPLE_RATE,
        "sample_clock_baseline_samples": target.sample_clock_baseline_samples,
        "sample_clock_drift_samples": drift_samples,
        "sample_clock_drift_ms": drift_samples * 1000.0 / SAMPLE_RATE,
    }


class RuntimeSyncService:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.ring = RingBuffer(capacity_sec=max(20.0, args.cadence_sec * 2.0))
        self.capture = ParecordCapture(self.ring, args.mic_source, args.mic_source_prefix)
        self.detector = EnvelopeDetector(self.ring)
        self.state = RuntimeSyncState()
        self.stop_event = asyncio.Event()
        self.loop_task: Optional[asyncio.Task[None]] = None
        self.slice5_actuator: Optional[SpeakerActuator] = None
        self._slice4_observe_enabled = bool(getattr(args, "slice4_observe", False))
        self.slice4_observer: Optional[ObservationWriter] = (
            ObservationWriter(Path(args.slice4_observation_path))
            if self._slice4_observe_enabled
            else None
        )

    async def run(self) -> None:
        with self.slice4_observer if self.slice4_observer is not None else contextlib.nullcontext():
            await self.capture.start()
            await self.start_measurement()
            await self.stop_event.wait()
            if self.loop_task:
                self.loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.loop_task
            await self.capture.stop()

    async def start_measurement(self) -> dict[str, Any]:
        if self.loop_task and not self.loop_task.done():
            return {"ok": True, "state": "already_running"}
        self.state.measuring = True
        self.loop_task = asyncio.create_task(self._measurement_loop())
        return {"ok": True, "state": "started"}

    def emergency_stop(self) -> None:
        if self.slice5_actuator is not None:
            self.slice5_actuator.emergency_stop()

    async def _measurement_loop(self) -> None:
        await self.detector.warmup(self.args.warmup_sec)
        while self.state.measuring:
            previous = {target.mac: target for target in self.state.targets}
            discovered = discover_active_speakers(limit=self.args.max_speakers)
            for target in discovered:
                if target.mac in previous:
                    target.stable_count = previous[target.mac].stable_count
                    target.last_latency_ms = previous[target.mac].last_latency_ms
                    target.latency_history_ms = list(previous[target.mac].latency_history_ms)
                    target.sample_clock_baseline_samples = previous[target.mac].sample_clock_baseline_samples
                    target.last_sample_clock_delta_samples = previous[
                        target.mac
                    ].last_sample_clock_delta_samples
                    target.pattern_clock_reject_count = previous[target.mac].pattern_clock_reject_count
                    target.clock_prior_reset_remaining = previous[target.mac].clock_prior_reset_remaining
            self.state.targets = discovered
            self._sync_slice5_actuator(discovered)
            if not self.state.targets:
                _emit("measurement_idle", reason="no_active_speakers")
                await asyncio.sleep(5.0)
                continue
            for target in list(self.state.targets):
                if not self.state.measuring:
                    break
                await self._measure_once(target)
                await asyncio.sleep(self.args.cadence_sec)
            self.state.cycles += 1

    def _sync_slice5_actuator(self, targets: list[SpeakerTarget]) -> None:
        if getattr(self.args, "detector_mode", None) != "pattern":
            return
        sockets = {target.mac: target.socket_path for target in targets}
        if self.slice5_actuator is None:
            self.slice5_actuator = SpeakerActuator(sockets)
            register_ble_stop_callback(self.slice5_actuator.emergency_stop)
            _emit("slice5_actuator_started", speaker_macs=sorted(sockets), burst_amp_x1000=BURST_AMP_X1000)
            return
        self.slice5_actuator.sync_sockets(sockets)

    async def _measure_once(self, target: SpeakerTarget) -> None:
        if self.args.detector_mode == "pattern":
            await self._measure_pattern(target)
            return
        query = _send_filter_command(target.socket_path, "query") or {}
        target_delay_samples = int(query.get("target_delay_samples") or 0)
        target_delay_ms = (target_delay_samples / SAMPLE_RATE) * 1000.0
        _send_filter_command(target.socket_path, "query_emit_timestamps")
        emit_issue_monotonic = time.monotonic()
        emit_payload = (
            f"emit_burst {int(round(self.args.freq_hz * 10))} "
            f"{int(self.args.duration_ms)} {int(round(self.args.amplitude * 1000))}"
        )
        ack = _send_filter_command(target.socket_path, emit_payload)
        emit_monotonic = time.monotonic()
        _emit(
            "burst_emit",
            mac=target.mac,
            socket_path=str(target.socket_path),
            emit_issue_monotonic=emit_issue_monotonic,
            emit_monotonic=emit_monotonic,
            filter_query=query,
            slider_target_delay_samples=target_delay_samples,
            slider_target_delay_ms=target_delay_ms,
            ack=ack,
        )
        if not ack or not ack.get("ok"):
            _emit("burst_emit_failed", mac=target.mac, ack=ack)
            return

        expected_arrival = emit_monotonic + (target_delay_samples / SAMPLE_RATE) + (self.args.bt_codec_latency_ms / 1000.0)
        margin_ms = STABLE_WINDOW_MARGIN_MS if target.stable_count >= STABLE_MEASUREMENT_COUNT else INITIAL_WINDOW_MARGIN_MS
        detect_start = expected_arrival - (margin_ms / 1000.0)
        detect_end = expected_arrival + (margin_ms / 1000.0) + (self.args.duration_ms / 1000.0)
        await asyncio.sleep(max(0.0, detect_end - time.monotonic()))

        entries = (_send_filter_command(target.socket_path, "query_emit_timestamps") or {}).get("entries", [])
        detection = await self.detector.detect(detect_start, detect_end, mode=self.args.detector_mode)
        if not detection:
            target.stable_count = 0
            _emit(
                "burst_missed",
                mac=target.mac,
                emit_monotonic=emit_monotonic,
                expected_arrival_monotonic=expected_arrival,
                detect_start_monotonic=detect_start,
                detect_end_monotonic=detect_end,
                slider_target_delay_samples=target_delay_samples,
                frame_entries=entries,
            )
            return

        latency_ms = (detection["arrival_monotonic"] - emit_monotonic) * 1000.0
        target.stable_count += 1
        target.last_latency_ms = latency_ms
        sample_clock = _sample_clock_fields(target, detection, entries)
        _emit(
            "burst_arrival",
            mac=target.mac,
            emit_monotonic=emit_monotonic,
            arrival_monotonic=detection["arrival_monotonic"],
            detector_mode=detection.get("detector_mode", self.args.detector_mode),
            expected_arrival_monotonic=expected_arrival,
            latency_ms=latency_ms,
            slider_target_delay_samples=target_delay_samples,
            slider_target_delay_ms=target_delay_ms,
            peak_power_db=detection["peak_power_db"],
            noise_floor_db=detection["noise_floor_db"],
            snr_db=detection["snr_db"],
            candidate_count=detection.get("candidate_count"),
            frame_entries=entries,
            stable_count=target.stable_count,
            **sample_clock,
        )


    async def _measure_pattern(self, target: SpeakerTarget) -> None:
        query = _send_filter_command(target.socket_path, "query") or {}
        target_delay_samples = int(query.get("target_delay_samples") or 0)
        target_delay_ms = (target_delay_samples / SAMPLE_RATE) * 1000.0
        current_filter_delay_ms = _current_filter_delay_ms(query, target_delay_ms)
        _send_filter_command(target.socket_path, "query_emit_timestamps")

        emit_records: list[dict[str, Any]] = []
        emit_payload = (
            f"emit_burst {int(round(self.args.freq_hz * 10))} "
            f"{int(self.args.duration_ms)} {int(round(self.args.amplitude * 1000))}"
        )
        pattern_gap_sec = max(
            self.args.pattern_gap_ms / 1000.0,
            (target_delay_samples / SAMPLE_RATE) + (self.args.duration_ms / 1000.0) + 0.020,
        )
        for pattern_index in range(self.args.pattern_bursts):
            emit_issue_monotonic = time.monotonic()
            ack = _send_filter_command(target.socket_path, emit_payload)
            emit_monotonic = time.monotonic()
            record = {
                "pattern_index": pattern_index,
                "emit_issue_monotonic": emit_issue_monotonic,
                "emit_monotonic": emit_monotonic,
                "ack": ack,
            }
            emit_records.append(record)
            _emit(
                "burst_pattern_emit",
                mac=target.mac,
                socket_path=str(target.socket_path),
                pattern_index=pattern_index,
                pattern_bursts=self.args.pattern_bursts,
                emit_issue_monotonic=emit_issue_monotonic,
                emit_monotonic=emit_monotonic,
                filter_query=query if pattern_index == 0 else None,
                slider_target_delay_samples=target_delay_samples,
                slider_target_delay_ms=target_delay_ms,
                pattern_gap_sec=pattern_gap_sec,
                ack=ack,
            )
            if not ack or not ack.get("ok"):
                _emit("burst_emit_failed", mac=target.mac, ack=ack, pattern_index=pattern_index)
                return
            if pattern_index < self.args.pattern_bursts - 1:
                await asyncio.sleep(pattern_gap_sec)

        margin_ms = STABLE_WINDOW_MARGIN_MS if target.stable_count >= STABLE_MEASUREMENT_COUNT else INITIAL_WINDOW_MARGIN_MS
        first_expected = (
            emit_records[0]["emit_monotonic"]
            + (target_delay_samples / SAMPLE_RATE)
            + (self.args.bt_codec_latency_ms / 1000.0)
        )
        last_expected = (
            emit_records[-1]["emit_monotonic"]
            + (target_delay_samples / SAMPLE_RATE)
            + (self.args.bt_codec_latency_ms / 1000.0)
        )
        detect_start = first_expected - (margin_ms / 1000.0)
        detect_end = last_expected + (margin_ms / 1000.0) + (self.args.duration_ms / 1000.0)
        await asyncio.sleep(max(0.0, detect_end - time.monotonic()))

        entries = (_send_filter_command(target.socket_path, "query_emit_timestamps") or {}).get("entries", [])
        emit_entries = sorted(
            [entry for entry in entries if isinstance(entry, dict) and "frame_index" in entry],
            key=lambda entry: int(entry["frame_index"]),
        )
        if len(emit_entries) < self.args.pattern_bursts:
            target.stable_count = 0
            self._apply_slice5_missed_burst(target, current_filter_delay_ms=current_filter_delay_ms)
            self._record_slice4_missed_burst(
                target,
                current_filter_delay_ms=current_filter_delay_ms,
                pattern_state_snapshot={
                    "reason": "missing_emit_timestamps",
                    "expected_emit_count": self.args.pattern_bursts,
                    "emit_entry_count": len(emit_entries),
                    "detect_start_monotonic": detect_start,
                    "detect_end_monotonic": detect_end,
                    "stable_count": target.stable_count,
                },
            )
            _emit(
                "burst_pattern_missed",
                mac=target.mac,
                reason="missing_emit_timestamps",
                expected_emit_count=self.args.pattern_bursts,
                emit_entry_count=len(emit_entries),
                frame_entries=entries,
                detect_start_monotonic=detect_start,
                detect_end_monotonic=detect_end,
            )
            return

        emit_frame_indices = [int(entry["frame_index"]) for entry in emit_entries[: self.args.pattern_bursts]]
        clock_prior_delta_samples = (
            None if target.clock_prior_reset_remaining > 0 else target.last_sample_clock_delta_samples
        )
        if target.clock_prior_reset_remaining > 0:
            target.clock_prior_reset_remaining -= 1
        analysis = await self.detector.analyze_pattern(
            detect_start,
            detect_end,
            emit_frame_indices,
            tolerance_ms=self.args.pattern_tolerance_ms,
            expected_delta_samples=clock_prior_delta_samples,
            clock_tolerance_ms=self.args.pattern_clock_tolerance_ms,
            min_snr_db=self.args.pattern_min_snr_db,
            landmark=self.args.pattern_landmark,
            carrier_hz=self.args.freq_hz,
        )
        detection = analysis.get("selected")
        if not detection:
            target.stable_count = 0
            reject_reason = analysis.get("reject_reason", "pattern_not_matched")
            if reject_reason == "clock_prior_mismatch":
                target.pattern_clock_reject_count += 1
            else:
                target.pattern_clock_reject_count = 0
            pattern_clock_reject_count = target.pattern_clock_reject_count
            reset_clock_prior = pattern_clock_reject_count >= 3
            if reset_clock_prior:
                target.sample_clock_baseline_samples = None
                target.last_sample_clock_delta_samples = None
                target.pattern_clock_reject_count = 0
            self._apply_slice5_missed_burst(target, current_filter_delay_ms=current_filter_delay_ms)
            self._record_slice4_missed_burst(
                target,
                current_filter_delay_ms=current_filter_delay_ms,
                pattern_state_snapshot={
                    "reason": reject_reason,
                    "emit_frame_indices": emit_frame_indices,
                    "detect_start_monotonic": detect_start,
                    "detect_end_monotonic": detect_end,
                    "pattern_clock_reject_count": pattern_clock_reject_count,
                    "reset_clock_prior": reset_clock_prior,
                    "stable_count": target.stable_count,
                    "analysis": analysis,
                },
            )
            _emit(
                "burst_pattern_missed",
                mac=target.mac,
                reason=reject_reason,
                emit_frame_indices=emit_frame_indices,
                frame_entries=entries,
                detect_start_monotonic=detect_start,
                detect_end_monotonic=detect_end,
                pattern_clock_reject_count=pattern_clock_reject_count,
                reset_clock_prior=reset_clock_prior,
                clock_prior_reset_remaining=target.clock_prior_reset_remaining,
                **analysis,
            )
            return

        latency_ms = (detection["arrival_monotonic"] - emit_records[0]["emit_monotonic"]) * 1000.0
        target.stable_count += 1
        target.pattern_clock_reject_count = 0
        target.last_latency_ms = latency_ms
        target.latency_history_ms.append(latency_ms)
        if len(target.latency_history_ms) > SLICE4_HISTORY_LIMIT:
            target.latency_history_ms = target.latency_history_ms[-SLICE4_HISTORY_LIMIT:]
        sample_clock = _sample_clock_fields(target, detection, [emit_entries[0]])
        _emit(
            "burst_pattern_arrival",
            mac=target.mac,
            emit_monotonic=emit_records[0]["emit_monotonic"],
            arrival_monotonic=detection["arrival_monotonic"],
            detector_mode="pattern",
            latency_ms=latency_ms,
            slider_target_delay_samples=target_delay_samples,
            slider_target_delay_ms=target_delay_ms,
            peak_power_db=detection["peak_power_db"],
            noise_floor_db=detection["noise_floor_db"],
            snr_db=detection["snr_db"],
            candidate_count=detection.get("candidate_count"),
            matched_arrival_sample_indices=detection.get("matched_arrival_sample_indices"),
            matched_error_ms=detection.get("matched_error_ms"),
            matched_landmark_offset_ms=detection.get("matched_landmark_offset_ms"),
            pattern_mean_abs_error_ms=detection.get("pattern_mean_abs_error_ms"),
            pattern_max_abs_error_ms=detection.get("pattern_max_abs_error_ms"),
            pattern_min_snr_db=detection.get("pattern_min_snr_db"),
            pattern_landmark=detection.get("pattern_landmark"),
            pattern_carrier_hz=detection.get("pattern_carrier_hz"),
            pattern_clock_delta_spread_ms=detection.get("pattern_clock_delta_spread_ms"),
            pattern_selection_reason=detection.get("pattern_selection_reason"),
            pattern_match_count=detection.get("pattern_match_count"),
            pattern_rejected_by_clock_count=detection.get("pattern_rejected_by_clock_count"),
            pattern_clock_prior_delta_samples=detection.get("pattern_clock_prior_delta_samples"),
            pattern_clock_prior_error_ms=detection.get("pattern_clock_prior_error_ms"),
            pattern_clock_prior_tolerance_ms=detection.get("pattern_clock_prior_tolerance_ms"),
            clock_prior_reset_remaining=target.clock_prior_reset_remaining,
            envelope_noise_floor_db=detection.get("envelope_noise_floor_db"),
            envelope_threshold_db=detection.get("envelope_threshold_db"),
            envelope_peak_db=detection.get("envelope_peak_db"),
            envelope_peak_snr_db=detection.get("envelope_peak_snr_db"),
            sample_clock_anchor_sample_index=detection.get("sample_clock_anchor_sample_index"),
            sample_clock_anchor_monotonic=detection.get("sample_clock_anchor_monotonic"),
            emit_frame_indices=emit_frame_indices,
            frame_entries=entries,
            stable_count=target.stable_count,
            **sample_clock,
        )
        actuation_result = self._apply_slice5_proposal(
            target,
            current_filter_delay_ms=current_filter_delay_ms,
            measured_latency_ms=latency_ms,
            target_total_ms=current_filter_delay_ms + float(self.args.bt_codec_latency_ms),
        )
        if actuation_result is not None and actuation_result.clock_prior_reset:
            target.last_sample_clock_delta_samples = None
            target.clock_prior_reset_remaining = CLOCK_PRIOR_RESET_CYCLES
        self._record_slice4_observation(
            target,
            measured_latency_ms=latency_ms,
            current_filter_delay_ms=current_filter_delay_ms,
            snr_db=float(detection["snr_db"]),
            pattern_state_snapshot={
                "stable_count": target.stable_count,
                "sample_clock": sample_clock,
                "detection": {
                    "pattern_mean_abs_error_ms": detection.get("pattern_mean_abs_error_ms"),
                    "pattern_clock_delta_spread_ms": detection.get("pattern_clock_delta_spread_ms"),
                    "pattern_selection_reason": detection.get("pattern_selection_reason"),
                    "pattern_match_count": detection.get("pattern_match_count"),
                    "pattern_rejected_by_clock_count": detection.get("pattern_rejected_by_clock_count"),
                },
                "actuation": (
                    None
                    if actuation_result is None
                    else {
                        "action": actuation_result.action,
                        "delta_ms": actuation_result.delta_ms,
                        "clock_prior_reset": actuation_result.clock_prior_reset,
                    }
                ),
            },
            actuation_result=actuation_result,
        )

    def _record_slice4_missed_burst(
        self,
        target: SpeakerTarget,
        *,
        current_filter_delay_ms: float,
        pattern_state_snapshot: dict[str, Any],
    ) -> None:
        if self.slice4_observer is None:
            return
        self.slice4_observer.write_missed_burst(
            speaker_id=target.mac,
            current_filter_delay_ms=current_filter_delay_ms,
            history_snapshot=list(target.latency_history_ms[-SLICE4_HISTORY_LIMIT:]),
            pattern_state_snapshot={
                **self._pattern_state_base(target),
                **pattern_state_snapshot,
            },
        )

    def _apply_slice5_missed_burst(
        self,
        target: SpeakerTarget,
        *,
        current_filter_delay_ms: float,
    ) -> Optional[ActuationResult]:
        if self.slice5_actuator is None:
            return None
        return self.slice5_actuator.apply(
            target.mac,
            None,
            None,
            current_filter_delay_ms,
            missed_burst=True,
        )

    def _apply_slice5_proposal(
        self,
        target: SpeakerTarget,
        *,
        current_filter_delay_ms: float,
        measured_latency_ms: float,
        target_total_ms: float,
    ) -> Optional[ActuationResult]:
        if self.slice5_actuator is None:
            return None
        return self.slice5_actuator.apply(
            target.mac,
            measured_latency_ms,
            target_total_ms,
            current_filter_delay_ms,
        )

    def _record_slice4_observation(
        self,
        target: SpeakerTarget,
        *,
        measured_latency_ms: float,
        current_filter_delay_ms: float,
        snr_db: float,
        pattern_state_snapshot: dict[str, Any],
        actuation_result: Optional[ActuationResult] = None,
    ) -> None:
        if self.slice4_observer is None:
            return
        proposed_adjustment_ppm = math.nan
        confidence = 1.0 if actuation_result is not None and actuation_result.action != "missed" else 0.0
        self.slice4_observer.write_observation(
            speaker_id=target.mac,
            measured_latency_ms=measured_latency_ms,
            history_snapshot=list(target.latency_history_ms[-SLICE4_HISTORY_LIMIT:]),
            pattern_state_snapshot={
                **self._pattern_state_base(target),
                **pattern_state_snapshot,
            },
            proposed_adjustment_ppm=proposed_adjustment_ppm,
            actuation_applied_ppm=(
                actuation_result.delta_ms if actuation_result is not None else 0.0
            ),
            confidence=confidence,
            current_filter_delay_ms=current_filter_delay_ms,
            missed_burst=False,
            snr_db=snr_db,
        )

    def _pattern_state_base(self, target: SpeakerTarget) -> dict[str, Any]:
        baseline_latency_ms = None
        if self.slice5_actuator is not None:
            baseline_latency_ms = self.slice5_actuator.baseline_for(target.mac)
        return {
            "stable_count": target.stable_count,
            "sample_clock_baseline_samples": target.sample_clock_baseline_samples,
            "last_sample_clock_delta_samples": target.last_sample_clock_delta_samples,
            "pattern_clock_reject_count": target.pattern_clock_reject_count,
            "baseline_latency_ms": baseline_latency_ms,
            "clock_prior_reset_remaining": target.clock_prior_reset_remaining,
        }


def _current_filter_delay_ms(query: dict[str, Any], fallback_delay_ms: float) -> float:
    current_x100 = query.get("current_delay_samples_x100")
    if current_x100 is not None:
        try:
            return (float(current_x100) / 100.0 / SAMPLE_RATE) * 1000.0
        except (TypeError, ValueError):
            pass
    current_samples = query.get("current_delay_samples")
    if current_samples is not None:
        try:
            return (float(current_samples) / SAMPLE_RATE) * 1000.0
        except (TypeError, ValueError):
            pass
    return fallback_delay_ms


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mic-source", help="Exact PulseAudio/PipeWire source name for parecord")
    parser.add_argument("--mic-source-prefix", default=DEFAULT_MIC_SOURCE_PREFIX)
    parser.add_argument("--cadence-sec", type=float, default=DEFAULT_CADENCE_SEC)
    parser.add_argument("--warmup-sec", type=float, default=DEFAULT_WARMUP_SEC)
    parser.add_argument("--max-speakers", type=int, default=2)
    parser.add_argument("--freq-hz", type=float, default=DEFAULT_FREQ_HZ)
    parser.add_argument("--duration-ms", type=int, default=DEFAULT_DURATION_MS)
    parser.add_argument("--amplitude", type=float, default=DEFAULT_AMPLITUDE)
    parser.add_argument("--bt-codec-latency-ms", type=float, default=DEFAULT_BT_CODEC_LATENCY_MS)
    parser.add_argument(
        "--detector-mode",
        choices=("peak", "onset", "pattern"),
        default="peak",
        help="Measurement detector: legacy peak window, onset threshold, or multi-burst pattern",
    )
    parser.add_argument(
        "--pattern-bursts",
        type=int,
        default=3,
        help="Number of bursts to emit per speaker when --detector-mode=pattern",
    )
    parser.add_argument(
        "--pattern-gap-ms",
        type=float,
        default=300.0,
        help="Minimum wall-clock gap between pattern burst commands; expanded when filter delay is longer",
    )
    parser.add_argument(
        "--pattern-tolerance-ms",
        type=float,
        default=PATTERN_TOLERANCE_MS,
        help="Maximum per-burst timing mismatch allowed when matching a pattern",
    )
    parser.add_argument(
        "--pattern-clock-tolerance-ms",
        type=float,
        default=PATTERN_CLOCK_TOLERANCE_MS,
        help="Maximum cycle-to-cycle sample-clock delta jump allowed for a pattern match",
    )
    parser.add_argument(
        "--pattern-min-snr-db",
        type=float,
        default=PATTERN_MIN_SNR_DB,
        help="Minimum ultrasonic-band SNR for onset candidates in pattern mode",
    )
    parser.add_argument(
        "--pattern-landmark",
        choices=("envelope", "onset"),
        default=PATTERN_LANDMARK,
        help="Mic-side timing landmark for pattern mode",
    )
    parser.add_argument(
        "--slice4-observe",
        action="store_true",
        default=slice4_observe_from_env(),
        help="Append observe-only Slice 4 proposal rows without actuating",
    )
    parser.add_argument(
        "--slice4-observation-path",
        default=str(DEFAULT_OBSERVATION_PATH),
        help=argparse.SUPPRESS,
    )
    return parser


async def _amain(argv: Optional[Iterable[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if args.slice4_observe:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        args.detector_mode = "pattern"
    if args.detector_mode == "pattern" and args.pattern_bursts < 2:
        raise SystemExit("--pattern-bursts must be >= 2 when --detector-mode=pattern")
    service = RuntimeSyncService(args)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, service.stop_event.set)
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGUSR1, service.emergency_stop)
    _emit("service_starting", args=vars(args))
    try:
        await service.run()
    except Exception as exc:  # noqa: BLE001
        _emit("service_fatal", error=repr(exc))
        return 1
    _emit("service_stopped")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
