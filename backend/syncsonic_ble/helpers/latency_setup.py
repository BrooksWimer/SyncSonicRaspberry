from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import tempfile
import time
import wave
from math import ceil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from syncsonic_ble.helpers.actuation import get_actuation_manager
from syncsonic_ble.helpers.pipewire_control_plane import publish_output_mix, read_control_state
from syncsonic_ble.helpers.pipewire_transport import resolve_pipewire_output_name

try:
    from syncsonic_ble.utils.logging_conf import get_logger
    log = get_logger(__name__)
except Exception:
    import logging

    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger(__name__)


DEFAULT_PULSE_SERVER = "unix:/run/syncsonic/pulse/native"
SYNCSONIC_ENV = "/etc/default/syncsonic"
DEBUG_DIR = Path(tempfile.gettempdir()) / "syncsonic_debug"

SETUP_TONE_FREQ_HZ = 1800.0
SETUP_TONE_DURATION_SEC = 0.12
SETUP_SAMPLE_RATE = 48000
RECORD_SAMPLE_RATE = 48000
RECORD_CHANNELS = 1
RECORD_PRE_ROLL_SEC = 0.40
RECORD_CAPTURE_SEC = 3.00
DETECTION_GUARD_SEC = 0.10
TARGET_MARGIN_MS = 60.0


@dataclass
class Measurement:
    mac: str
    sink_name: str
    onset_ms: float
    peak_ms: float
    threshold: float
    rms: float
    png_path: str = ""
    wav_path: str = ""


def _load_syncsonic_env() -> None:
    if not os.path.exists(SYNCSONIC_ENV):
        return
    with open(SYNCSONIC_ENV, "r", encoding="ascii", errors="ignore") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith("export ") and "=" in line:
                key, _, value = line[7:].partition("=")
                os.environ[key] = value


def _pulse_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PULSE_SERVER", DEFAULT_PULSE_SERVER)
    return env


def _connected_bluetooth_macs() -> List[str]:
    result = subprocess.run(
        ["pactl", "list", "sinks", "short"],
        capture_output=True,
        text=True,
        env=_pulse_env(),
    )
    if result.returncode != 0:
        return []

    macs: List[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        sink_name = parts[1]
        token: Optional[str] = None
        if sink_name.startswith("bluez_output."):
            token = sink_name.split(".", 1)[1].split(".", 1)[0]
        elif sink_name.startswith("bluez_sink."):
            token = sink_name.split(".", 1)[1].split(".", 1)[0]
        if not token:
            continue
        mac = token.replace("_", ":").upper()
        if mac not in macs:
            macs.append(mac)
    return macs


def _make_setup_tone(path: str) -> None:
    total_samples = int(SETUP_SAMPLE_RATE * SETUP_TONE_DURATION_SEC)
    fade_samples = max(1, int(0.01 * SETUP_SAMPLE_RATE))
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SETUP_SAMPLE_RATE)
        for i in range(total_samples):
            t = i / SETUP_SAMPLE_RATE
            value = math.sin(2.0 * math.pi * SETUP_TONE_FREQ_HZ * t)
            if i < fade_samples:
                value *= i / fade_samples
            elif i >= total_samples - fade_samples:
                value *= max(0.0, (total_samples - 1 - i) / fade_samples)
            sample = max(-32767, min(32767, int(value * 0.85 * 32767)))
            wav.writeframes(struct.pack("<h", sample))


def _record_measurement(output_wav: str) -> subprocess.Popen:
    duration_sec = max(1, int(ceil(RECORD_PRE_ROLL_SEC + RECORD_CAPTURE_SEC + 0.5)))
    return subprocess.Popen(
        [
            "arecord",
            "-q",
            "-f",
            "S16_LE",
            "-r",
            str(RECORD_SAMPLE_RATE),
            "-c",
            str(RECORD_CHANNELS),
            "-d",
            str(duration_sec),
            output_wav,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _snapshot_output_mix(macs: List[str]) -> Dict[str, Tuple[int, int]]:
    state = read_control_state()
    outputs = state.get("outputs", {})
    if not isinstance(outputs, dict):
        outputs = {}
    snapshot: Dict[str, Tuple[int, int]] = {}
    for mac in macs:
        cfg = outputs.get(mac.upper(), {})
        if not isinstance(cfg, dict):
            cfg = {}
        left = int(cfg.get("left_percent", 100))
        right = int(cfg.get("right_percent", 100))
        snapshot[mac.upper()] = (left, right)
    return snapshot


def _set_isolated_measurement_mix(target_mac: str, macs: List[str]) -> Dict[str, Tuple[int, int]]:
    snapshot = _snapshot_output_mix(macs)
    target_mac = target_mac.upper()
    for mac in macs:
        current_left, current_right = snapshot.get(mac.upper(), (100, 100))
        if mac.upper() == target_mac:
            publish_output_mix(mac, left_percent=current_left, right_percent=current_right)
        else:
            publish_output_mix(mac, left_percent=0, right_percent=0)
    time.sleep(0.15)
    return snapshot


def _restore_output_mix(snapshot: Dict[str, Tuple[int, int]]) -> None:
    for mac, (left, right) in snapshot.items():
        publish_output_mix(mac, left_percent=left, right_percent=right)
    time.sleep(0.10)


def _play_setup_tone(tone_wav: str) -> bool:
    result = subprocess.run(
        ["paplay", "-d", "virtual_out", tone_wav],
        capture_output=True,
        text=True,
        env=_pulse_env(),
    )
    if result.returncode != 0:
        log.warning("paplay failed via virtual_out: %s", (result.stderr or "").strip())
        return False
    return True


def _read_wav_mono_f32(path: str) -> Tuple[np.ndarray, int]:
    with wave.open(path, "rb") as wav:
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        width = wav.getsampwidth()
        frame_count = wav.getnframes()
        raw = wav.readframes(frame_count)
    if width != 2:
        raise RuntimeError(f"Unsupported wav sample width {width}")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate


def _detect_onset_ms(samples: np.ndarray, sample_rate: int) -> Tuple[float, float, float, float]:
    if samples.size == 0:
        raise RuntimeError("empty recording")

    samples = samples - float(np.mean(samples))
    abs_signal = np.abs(samples)
    win = max(8, int(sample_rate * 0.008))
    kernel = np.ones(win, dtype=np.float32) / float(win)
    envelope = np.convolve(abs_signal, kernel, mode="same")

    noise_end = max(1, int(sample_rate * RECORD_PRE_ROLL_SEC * 0.6))
    noise_slice = envelope[:noise_end]
    noise_floor = float(np.mean(noise_slice))
    noise_std = float(np.std(noise_slice))
    threshold = max(noise_floor + (noise_std * 8.0), noise_floor * 4.0, 0.01)

    search_start = int(sample_rate * DETECTION_GUARD_SEC)
    peak_index = int(np.argmax(envelope[search_start:])) + search_start
    above = np.where(envelope[search_start:] >= threshold)[0]
    if above.size == 0:
        raise RuntimeError("setup tone onset not detected")
    onset_index = int(above[0]) + search_start

    onset_ms = (onset_index / sample_rate) * 1000.0
    peak_ms = (peak_index / sample_rate) * 1000.0
    rms = float(np.sqrt(np.mean(samples * samples)))
    return onset_ms, peak_ms, threshold, rms


def _write_debug_plot(samples: np.ndarray, sample_rate: int, onset_ms: float, peak_ms: float, mac: str) -> str:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    png_path = DEBUG_DIR / f"setup_{mac.replace(':', '_').lower()}.png"
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        times = np.arange(samples.size, dtype=np.float32) / float(sample_rate)
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.specgram(samples, NFFT=1024, Fs=sample_rate, noverlap=768, cmap="magma")
        ax.axvline(onset_ms / 1000.0, color="cyan", linewidth=2, label="onset")
        ax.axvline(peak_ms / 1000.0, color="lime", linewidth=2, label="peak")
        ax.set_title(f"SyncSonic Setup Measurement {mac}")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Frequency (Hz)")
        ax.set_ylim(0, 6000)
        ax.legend(loc="upper right")
        fig.tight_layout()
        fig.savefig(png_path)
        plt.close(fig)
        return str(png_path)
    except Exception as exc:
        log.debug("Failed to write setup spectrogram for %s: %s", mac, exc)
        return ""


def _measure_single_output(mac: str, tone_wav: str, all_macs: List[str]) -> Measurement:
    sink_name = resolve_pipewire_output_name(mac)
    if not sink_name:
        raise RuntimeError(f"output sink not found for {mac}")

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = DEBUG_DIR / f"setup_{mac.replace(':', '_').lower()}.wav"

    snapshot = _set_isolated_measurement_mix(mac, all_macs)
    recorder = _record_measurement(str(wav_path))
    try:
        time.sleep(RECORD_PRE_ROLL_SEC)
        if not _play_setup_tone(tone_wav):
            raise RuntimeError(f"setup tone play failed for {mac}")
        time.sleep(RECORD_CAPTURE_SEC)
    finally:
        try:
            recorder.wait(timeout=3.0)
        except Exception:
            recorder.terminate()
            try:
                recorder.wait(timeout=1.0)
            except Exception:
                recorder.kill()
        _restore_output_mix(snapshot)
    meta_path = DEBUG_DIR / f"setup_{mac.replace(':', '_').lower()}.json"
    try:
        samples, sample_rate = _read_wav_mono_f32(str(wav_path))
        onset_ms, peak_ms, threshold, rms = _detect_onset_ms(samples, sample_rate)
        png_path = _write_debug_plot(samples, sample_rate, onset_ms, peak_ms, mac)
        measurement = Measurement(
            mac=mac,
            sink_name=sink_name,
            onset_ms=round(onset_ms, 3),
            peak_ms=round(peak_ms, 3),
            threshold=round(threshold, 6),
            rms=round(rms, 6),
            png_path=png_path,
            wav_path=str(wav_path),
        )
        with open(meta_path, "w", encoding="ascii") as fh:
            json.dump(asdict(measurement), fh, indent=2, sort_keys=True)
        return measurement
    except Exception as exc:
        failure = {
            "mac": mac,
            "sink_name": sink_name,
            "wav_path": str(wav_path),
            "error": str(exc),
        }
        with open(meta_path, "w", encoding="ascii") as fh:
            json.dump(failure, fh, indent=2, sort_keys=True)
        raise


def run_end_to_end_setup() -> Tuple[bool, Dict[str, Any]]:
    _load_syncsonic_env()
    macs = _connected_bluetooth_macs()
    if not macs:
        return False, {"reason": "no_bluetooth_outputs"}

    fd, tone_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        _make_setup_tone(tone_path)
        measurements: List[Measurement] = []
        for mac in macs:
            measurements.append(_measure_single_output(mac, tone_path, macs))
    finally:
        try:
            os.remove(tone_path)
        except OSError:
            pass

    if len(measurements) < 1:
        return False, {"reason": "no_measurements"}

    manager = get_actuation_manager()
    target_onset_ms = max(item.onset_ms for item in measurements) + TARGET_MARGIN_MS
    applied: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for item in measurements:
        current_delay = float(manager.get_commanded_delay(item.mac))
        delta_ms = max(0.0, target_onset_ms - item.onset_ms)
        desired_delay = current_delay + delta_ms
        ok, snapshot = manager.apply_control_target(
            item.mac,
            delay_ms=desired_delay,
            rate_ppm=0.0,
            mode="manual",
        )
        entry = {
            "mac": item.mac,
            "measured_onset_ms": round(item.onset_ms, 3),
            "current_delay_ms": round(current_delay, 3),
            "correction_ms": round(delta_ms, 3),
            "target_delay_ms": round(desired_delay, 3),
        }
        if ok:
            entry["applied_delay_ms"] = snapshot.get("delay_applied_ms") if isinstance(snapshot, dict) else None
            applied.append(entry)
        else:
            failures.append(entry)

    result: Dict[str, Any] = {
        "reason": "end_to_end_setup_complete" if not failures else "end_to_end_setup_partial",
        "setup_done": not failures,
        "target_onset_ms": round(target_onset_ms, 3),
        "measured": [asdict(item) for item in measurements],
        "applied": applied,
        "failed": failures,
    }
    return not failures, result
