"""Open-loop ultrasonic runtime latency measurement service.

Slice 2 deliberately measures only: filter-resident ultrasonic burst
emission, USB mic capture, envelope detection, and JSON-lines journal
records. It does not feed measurements back into ``set_rate_ppm``.

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

import dbus
import numpy as np

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
MIN_SNR_DB = 12.0
SOCKET_TIMEOUT_SEC = 1.5


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
    samples: np.ndarray


class RingBuffer:
    """Timestamped in-memory sample ring fed by a long-running parecord task."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, capacity_sec: float = 20.0) -> None:
        self.sample_rate = sample_rate
        self.capacity_samples = int(sample_rate * capacity_sec)
        self._chunks: Deque[SampleChunk] = deque()
        self._samples = 0
        self._lock = asyncio.Lock()

    async def append(self, pcm: bytes, end_time: float) -> None:
        if not pcm:
            return
        usable = len(pcm) - (len(pcm) % SAMPLE_WIDTH_BYTES)
        if usable <= 0:
            return
        samples = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32) / 32768.0
        duration = len(samples) / self.sample_rate
        chunk = SampleChunk(start_time=end_time - duration, end_time=end_time, samples=samples)
        async with self._lock:
            self._chunks.append(chunk)
            self._samples += len(samples)
            while self._samples > self.capacity_samples and self._chunks:
                removed = self._chunks.popleft()
                self._samples -= len(removed.samples)

    async def read_window(self, start_time: float, end_time: float) -> tuple[np.ndarray, float]:
        async with self._lock:
            pieces: list[np.ndarray] = []
            first_time: Optional[float] = None
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
                pieces.append(chunk.samples[start_idx:end_idx])
        if not pieces:
            return np.zeros(0, dtype=np.float32), start_time
        return np.concatenate(pieces), first_time if first_time is not None else start_time


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

    async def detect(self, start_time: float, end_time: float) -> Optional[dict[str, float]]:
        samples, base_time = await self.ring.read_window(start_time, end_time)
        window = int(SAMPLE_RATE * WINDOW_MS / 1000.0)
        hop = int(SAMPLE_RATE * HOP_MS / 1000.0)
        if len(samples) < window:
            return None
        best_db = -200.0
        best_idx = 0
        for idx in range(0, len(samples) - window + 1, hop):
            power = self._band_power_db(samples[idx : idx + window])
            if power > best_db:
                best_db = power
                best_idx = idx
        snr_db = best_db - self.noise_floor_db
        if snr_db < MIN_SNR_DB:
            return None
        arrival_time = base_time + ((best_idx + (window / 2)) / SAMPLE_RATE)
        return {
            "arrival_monotonic": arrival_time,
            "peak_power_db": best_db,
            "noise_floor_db": self.noise_floor_db,
            "snr_db": snr_db,
        }

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


class RuntimeSyncService:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.ring = RingBuffer(capacity_sec=max(20.0, args.cadence_sec * 2.0))
        self.capture = ParecordCapture(self.ring, args.mic_source, args.mic_source_prefix)
        self.detector = EnvelopeDetector(self.ring)
        self.state = RuntimeSyncState()
        self.stop_event = asyncio.Event()
        self.loop_task: Optional[asyncio.Task[None]] = None

    async def run(self) -> None:
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

    async def _measurement_loop(self) -> None:
        await self.detector.warmup(self.args.warmup_sec)
        while self.state.measuring:
            previous = {target.mac: target for target in self.state.targets}
            discovered = discover_active_speakers(limit=self.args.max_speakers)
            for target in discovered:
                if target.mac in previous:
                    target.stable_count = previous[target.mac].stable_count
                    target.last_latency_ms = previous[target.mac].last_latency_ms
            self.state.targets = discovered
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

    async def _measure_once(self, target: SpeakerTarget) -> None:
        query = _send_filter_command(target.socket_path, "query") or {}
        target_delay_samples = int(query.get("target_delay_samples") or 0)
        target_delay_ms = (target_delay_samples / SAMPLE_RATE) * 1000.0
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
        detection = await self.detector.detect(detect_start, detect_end)
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
        _emit(
            "burst_arrival",
            mac=target.mac,
            emit_monotonic=emit_monotonic,
            arrival_monotonic=detection["arrival_monotonic"],
            expected_arrival_monotonic=expected_arrival,
            latency_ms=latency_ms,
            slider_target_delay_samples=target_delay_samples,
            slider_target_delay_ms=target_delay_ms,
            peak_power_db=detection["peak_power_db"],
            noise_floor_db=detection["noise_floor_db"],
            snr_db=detection["snr_db"],
            frame_entries=entries,
            stable_count=target.stable_count,
        )

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
    return parser


async def _amain(argv: Optional[Iterable[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    service = RuntimeSyncService(args)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, service.stop_event.set)
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
