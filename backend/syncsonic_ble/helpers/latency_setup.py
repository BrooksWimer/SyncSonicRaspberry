from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from math import ceil
from dataclasses import dataclass, asdict
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from syncsonic_ble.helpers.actuation import get_actuation_manager
from syncsonic_ble.helpers.pipewire_control_plane import get_transport_base_ms, publish_output_mix, read_control_state
from syncsonic_ble.helpers.pipewire_transport import resolve_pipewire_output_name
from syncsonic_ble.helpers.device_type_helpers import is_sonos
from syncsonic_ble.helpers.device_labels import format_device_label
from syncsonic_ble.helpers.sonos_controller import set_volume as sonos_set_volume, get_volume as sonos_get_volume

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
SETUP_TONE_DURATION_SEC = float(os.environ.get("SYNCSONIC_PROBE_DURATION_SEC", "0.22"))
SETUP_TONE_GAIN = float(os.environ.get("SYNCSONIC_SETUP_TONE_GAIN", "1.0"))
SETUP_SAMPLE_RATE = 48000
SETUP_CHIRP_START_HZ = float(os.environ.get("SYNCSONIC_PROBE_CHIRP_START_HZ", "220.0"))
SETUP_CHIRP_END_HZ = float(os.environ.get("SYNCSONIC_PROBE_CHIRP_END_HZ", "1400.0"))
RECORD_SAMPLE_RATE = 48000
RECORD_CHANNELS = 1
RECORD_PRE_ROLL_SEC = 0.40
RECORD_CAPTURE_SEC = 3.00
DETECTION_GUARD_SEC = 0.10
TARGET_MARGIN_MS = 60.0
PHASE1_PRE_ROLL_SEC = float(os.environ.get("SYNCSONIC_PHASE1_PRE_ROLL_SEC", "1.0"))
PHASE1_INTER_BURST_SEC = float(os.environ.get("SYNCSONIC_PHASE1_INTER_BURST_SEC", "2.0"))
PHASE1_POST_ROLL_SEC = float(os.environ.get("SYNCSONIC_PHASE1_POST_ROLL_SEC", "3.0"))
PHASE1_ISOLATION_SETTLE_SEC = float(os.environ.get("SYNCSONIC_PHASE1_ISOLATION_SETTLE_SEC", "0.2"))
PHASE1_CORR_PRE_SEC = float(os.environ.get("SYNCSONIC_PHASE1_CORR_PRE_SEC", "0.25"))
PHASE1_CORR_POST_SEC = float(os.environ.get("SYNCSONIC_PHASE1_CORR_POST_SEC", "1.2"))
PHASE1_CORR_MIN_SCORE = float(os.environ.get("SYNCSONIC_PHASE1_CORR_MIN_SCORE", "6.0"))
PHASE1_CORR_POST_SEC_SONOS = float(os.environ.get("SYNCSONIC_PHASE1_CORR_POST_SEC_SONOS", "10.0"))
PHASE1_CORR_MIN_SCORE_SONOS = float(os.environ.get("SYNCSONIC_PHASE1_CORR_MIN_SCORE_SONOS", "3.0"))
FRONTEND_SONOS_INTER_BURST_SEC = float(os.environ.get("SYNCSONIC_FRONTEND_SONOS_INTER_BURST_SEC", "10.0"))
FRONTEND_SONOS_POST_ROLL_SEC = float(os.environ.get("SYNCSONIC_FRONTEND_SONOS_POST_ROLL_SEC", "12.0"))
FRONTEND_STEP_OVERHEAD_SEC = float(os.environ.get("SYNCSONIC_FRONTEND_STEP_OVERHEAD_SEC", "1.5"))


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


@dataclass
class FrontendProbeSession:
    run_tag: int
    probe_order: List[str]
    wav_path: str
    png_path: str
    json_path: str
    start_monotonic: float
    planned_total_duration_sec: float
    planned_stop_monotonic: float
    recorder: Any
    send_events: List[Dict[str, Any]]


_FRONTEND_PROBE_LOCK = Lock()
_FRONTEND_PROBE_SESSION: Optional[FrontendProbeSession] = None


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
    source_wav = _find_probe_source_wav()
    if source_wav:
        try:
            shutil.copyfile(source_wav, path)
            return
        except Exception as exc:
            log.warning("Failed to copy probe wav from %s: %s; falling back to generated chirp", source_wav, exc)
    probe = _build_probe_waveform(SETUP_SAMPLE_RATE)
    pcm = np.clip(probe * 32767.0, -32767.0, 32767.0).astype("<i2")
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SETUP_SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())


def _find_probe_source_wav() -> Optional[str]:
    candidates = [
        os.environ.get("SYNCSONIC_PROBE_SOURCE_WAV", "").strip(),
        "/home/syncsonic/SyncSonicPi/frontend/assets/sound/beep.wav",
        "/home/syncsonic/SyncSonicPi/backend/assets/sound/beep.wav",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _build_probe_waveform(sample_rate: int) -> np.ndarray:
    total_samples = max(1, int(sample_rate * SETUP_TONE_DURATION_SEC))
    t = np.arange(total_samples, dtype=np.float64) / float(sample_rate)
    k = (SETUP_CHIRP_END_HZ - SETUP_CHIRP_START_HZ) / max(1e-6, SETUP_TONE_DURATION_SEC)
    phase = (2.0 * math.pi) * (SETUP_CHIRP_START_HZ * t + 0.5 * k * (t ** 2))
    chirp = np.sin(phase)
    harmonic_1 = np.sin((2.0 * phase) + 0.6)
    harmonic_2 = np.sin((3.0 * phase) + 1.1)
    click = np.zeros_like(chirp)
    click_len = max(1, int(0.004 * sample_rate))
    click[:click_len] = 1.0
    signal = (0.70 * chirp) + (0.20 * harmonic_1) + (0.08 * harmonic_2) + (0.10 * click)
    signal *= np.hanning(total_samples)
    signal *= float(SETUP_TONE_GAIN)
    return signal.astype(np.float32)


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


def _record_measurement_for_duration(output_wav: str, duration_sec: float) -> subprocess.Popen:
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
            str(max(1, int(ceil(duration_sec)))),
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
        left_raw = cfg.get("left_percent")
        right_raw = cfg.get("right_percent")
        if left_raw is None or right_raw is None:
            pactl_levels = _read_bt_sink_channel_volumes(mac)
            if pactl_levels is not None:
                left, right = pactl_levels
            else:
                continue
        else:
            left = int(left_raw)
            right = int(right_raw)
        snapshot[mac.upper()] = (left, right)
    return snapshot


def _read_bt_sink_channel_volumes(mac: str) -> Optional[Tuple[int, int]]:
    sink_name = resolve_pipewire_output_name(mac)
    if not sink_name:
        return None
    result = subprocess.run(
        ["pactl", "get-sink-volume", sink_name],
        capture_output=True,
        text=True,
        env=_pulse_env(),
    )
    if result.returncode != 0:
        return None
    # Example: "Volume: front-left: 65536 / 100% / 0.00 dB, front-right: 65536 / 100% / 0.00 dB"
    percents = [int(x) for x in re.findall(r"(\d+)%", result.stdout)]
    if not percents:
        return None
    if len(percents) == 1:
        return percents[0], percents[0]
    return percents[0], percents[1]


def _set_isolated_measurement_mix(target_mac: str, macs: List[str]) -> Dict[str, Tuple[int, int]]:
    snapshot = _snapshot_output_mix(macs)
    target_mac = target_mac.upper()
    for mac in macs:
        current = snapshot.get(mac.upper())
        if current is None:
            continue
        current_left, current_right = current
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


def _play_setup_tone_for_target(target_id: str, tone_wav: str) -> bool:
    if is_sonos(target_id):
        # Sonos playback injection is not implemented in this phase.
        log.warning("Probe playback to Sonos target is not implemented: %s", format_device_label(target_id))
        return False
    sink_name = resolve_pipewire_output_name(target_id)
    if not sink_name:
        log.warning("Probe sink not found for %s", format_device_label(target_id))
        return False
    result = subprocess.run(
        ["paplay", "-d", sink_name, tone_wav],
        capture_output=True,
        text=True,
        env=_pulse_env(),
    )
    if result.returncode != 0:
        log.warning(
            "paplay failed for %s via sink %s: %s",
            format_device_label(target_id),
            sink_name,
            (result.stderr or "").strip(),
        )
        return False
    return True


def _resample_linear(signal: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if signal.size == 0:
        return signal.astype(np.float32)
    if src_rate == dst_rate:
        return signal.astype(np.float32)
    duration = float(signal.size) / float(src_rate)
    out_size = max(1, int(round(duration * float(dst_rate))))
    src_t = np.linspace(0.0, duration, signal.size, endpoint=False, dtype=np.float64)
    dst_t = np.linspace(0.0, duration, out_size, endpoint=False, dtype=np.float64)
    out = np.interp(dst_t, src_t, signal.astype(np.float64))
    return out.astype(np.float32)


def _load_probe_template_waveform(path: str, target_sample_rate: int) -> np.ndarray:
    samples, src_rate = _read_wav_mono_f32(path)
    return _resample_linear(samples, src_rate, target_sample_rate)


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


def _detect_sequence_onsets_ms(
    samples: np.ndarray,
    sample_rate: int,
    *,
    send_events: List[Dict[str, Any]],
    template_waveform: Optional[np.ndarray] = None,
) -> List[Dict[str, float]]:
    if samples.size == 0 or not send_events:
        return []

    signal = samples.astype(np.float64)
    signal -= float(np.mean(signal))
    if template_waveform is None or template_waveform.size == 0:
        template = _build_probe_waveform(sample_rate).astype(np.float64)
    else:
        template = template_waveform.astype(np.float64)
    template -= float(np.mean(template))
    template_norm = float(np.linalg.norm(template))
    if template_norm <= 1e-9:
        return []
    template /= template_norm
    tpl_n = int(template.size)
    if signal.size < tpl_n:
        return []

    corr = np.convolve(signal, template[::-1], mode="valid")
    sq = signal * signal
    csum = np.concatenate(([0.0], np.cumsum(sq)))
    window_energy = csum[tpl_n:] - csum[:-tpl_n]
    denom = np.sqrt(np.maximum(window_energy, 1e-12))
    score = corr / denom

    baseline_len = min(score.size, max(1, int(sample_rate * max(0.4, PHASE1_PRE_ROLL_SEC * 0.8))))
    baseline = score[:baseline_len]
    base_med = float(np.median(baseline))
    mad = float(np.median(np.abs(baseline - base_med)))
    robust_sigma = max(1e-6, 1.4826 * mad)
    z = (score - base_med) / robust_sigma

    detections: List[Dict[str, float]] = []
    min_start_idx = 0
    for event in send_events:
        sent_sec = float(event.get("send_from_record_start_sec", 0.0))
        target_id = str(event.get("target_id", "")).strip()
        sonos_target = is_sonos(target_id)
        corr_post_sec = PHASE1_CORR_POST_SEC_SONOS if sonos_target else PHASE1_CORR_POST_SEC
        corr_min_score = PHASE1_CORR_MIN_SCORE_SONOS if sonos_target else PHASE1_CORR_MIN_SCORE
        center_idx = int(max(0.0, sent_sec) * sample_rate)
        lo = max(min_start_idx, center_idx - int(PHASE1_CORR_PRE_SEC * sample_rate))
        hi = min(z.size - 1, center_idx + int(corr_post_sec * sample_rate))
        if hi <= lo:
            continue
        local = z[lo : hi + 1]
        local_peak_offset = int(np.argmax(local))
        best_idx = lo + local_peak_offset
        best_z = float(z[best_idx])
        if best_z < corr_min_score:
            continue
        onset_ms = (best_idx / sample_rate) * 1000.0
        peak_ms = ((best_idx + (tpl_n // 2)) / sample_rate) * 1000.0
        detections.append(
            {
                "onset_ms": round(onset_ms, 3),
                "peak_ms": round(peak_ms, 3),
                "score_z": round(best_z, 3),
                "score": round(float(score[best_idx]), 6),
            }
        )
        min_start_idx = best_idx + max(1, int(0.05 * sample_rate))
    return detections


def _write_sequence_debug_plot(
    samples: np.ndarray,
    sample_rate: int,
    *,
    send_events: List[Dict[str, Any]],
    detections: List[Dict[str, float]],
    output_png: str,
) -> str:
    try:
        import warnings
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(12, 4))
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="divide by zero encountered in log10")
            ax.specgram(samples, NFFT=1024, Fs=sample_rate, noverlap=768, cmap="magma")
        for event in send_events:
            t = float(event.get("send_from_record_start_sec", 0.0))
            ax.axvline(t, color="cyan", linewidth=1.2, linestyle="--")
        for det in detections:
            t = float(det.get("onset_ms", 0.0)) / 1000.0
            ax.axvline(t, color="lime", linewidth=1.4)
        ax.set_title("SyncSonic Startup Probe: sent (cyan) vs detected (lime)")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Frequency (Hz)")
        ax.set_ylim(0, 6000)
        fig.tight_layout()
        fig.savefig(output_png)
        plt.close(fig)
        return output_png
    except Exception as exc:
        log.debug("Failed to write sequence spectrogram: %s", exc)
        return ""


def _snapshot_sonos_volumes(device_ids: List[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for device_id in device_ids:
        vol = sonos_get_volume(device_id)
        if vol is not None:
            out[device_id] = int(max(0, min(100, vol)))
    return out


def _apply_isolation_mix(
    target_id: str,
    *,
    bt_macs: List[str],
    sonos_ids: List[str],
    bt_snapshot: Dict[str, Tuple[int, int]],
    sonos_snapshot: Dict[str, int],
) -> None:
    target_upper = target_id.upper()
    for mac in bt_macs:
        current = bt_snapshot.get(mac.upper())
        if mac.upper() == target_upper:
            # Keep target exactly at user-set levels; do not override volume.
            if current is not None:
                target_left, target_right = current
                publish_output_mix(mac, left_percent=int(target_left), right_percent=int(target_right))
                log.info("[PROBE] target=%s bt_mix=%s/%s", format_device_label(mac), int(target_left), int(target_right))
            else:
                log.info("[PROBE] target=%s bt_mix=unchanged", format_device_label(mac))
        else:
            publish_output_mix(mac, left_percent=0, right_percent=0)
    for device_id in sonos_ids:
        if device_id not in sonos_snapshot:
            continue
        baseline = int(max(0, min(100, sonos_snapshot[device_id])))
        target_vol = baseline if device_id == target_id else 0
        sonos_set_volume(device_id, target_vol)
    time.sleep(PHASE1_ISOLATION_SETTLE_SEC)


def _restore_probe_mix(
    *,
    bt_snapshot: Dict[str, Tuple[int, int]],
    sonos_snapshot: Dict[str, int],
) -> None:
    for mac, (left, right) in bt_snapshot.items():
        publish_output_mix(mac, left_percent=left, right_percent=right)
    for device_id, vol in sonos_snapshot.items():
        sonos_set_volume(device_id, int(max(0, min(100, vol))))
    time.sleep(0.10)


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


def run_startup_probe_phase1(
    connected_ids: Optional[List[str]] = None,
    *,
    playback_callback: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Phase-1 startup probe: isolate speakers, capture mic data, no delay writes."""
    _load_syncsonic_env()
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    bt_macs: List[str] = []
    sonos_ids: List[str] = []
    if connected_ids:
        for raw in connected_ids:
            item = str(raw or "").strip()
            if not item:
                continue
            if is_sonos(item):
                sonos_ids.append(item)
            else:
                bt_macs.append(item.upper())
    else:
        bt_macs = _connected_bluetooth_macs()

    # Preserve order while de-duplicating
    bt_macs = list(dict.fromkeys(bt_macs))
    sonos_ids = list(dict.fromkeys(sonos_ids))
    probe_order: List[str] = [*bt_macs, *sonos_ids]
    if not probe_order:
        return False, {"reason": "no_connected_outputs"}

    fd, tone_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    run_tag = int(time.time())
    wav_path = DEBUG_DIR / f"startup_probe_{run_tag}.wav"
    png_path = DEBUG_DIR / f"startup_probe_{run_tag}.png"
    json_path = DEBUG_DIR / f"startup_probe_{run_tag}.json"
    total_duration = (
        PHASE1_PRE_ROLL_SEC
        + (len(probe_order) * PHASE1_INTER_BURST_SEC)
        + PHASE1_POST_ROLL_SEC
        + 1.0
    )
    bt_snapshot = _snapshot_output_mix(bt_macs)
    sonos_snapshot = _snapshot_sonos_volumes(sonos_ids)

    send_events: List[Dict[str, Any]] = []
    detections: List[Dict[str, float]] = []
    template_waveform: Optional[np.ndarray] = None
    try:
        _make_setup_tone(tone_path)
        template_waveform = _load_probe_template_waveform(tone_path, RECORD_SAMPLE_RATE)
        recorder = _record_measurement_for_duration(str(wav_path), total_duration)
        record_start_monotonic = time.monotonic()
        time.sleep(PHASE1_PRE_ROLL_SEC)
        for target_id in probe_order:
            _apply_isolation_mix(
                target_id,
                bt_macs=bt_macs,
                sonos_ids=sonos_ids,
                bt_snapshot=bt_snapshot,
                sonos_snapshot=sonos_snapshot,
            )
            send_mono = time.monotonic()
            send_events.append(
                {
                    "target_id": target_id,
                    "speaker": format_device_label(target_id),
                    "send_monotonic": round(send_mono, 6),
                    "send_from_record_start_sec": round(send_mono - record_start_monotonic, 6),
                }
            )
            played = False
            if playback_callback is not None:
                cue = {
                    "target_id": target_id,
                    "speaker": format_device_label(target_id),
                    "index": len(send_events) - 1,
                    "total": len(probe_order),
                    "play_beep": True,
                    "source": "phone",
                }
                try:
                    played = bool(playback_callback(cue))
                except Exception as exc:
                    log.warning("Phone playback callback failed for %s: %s", format_device_label(target_id), exc)
                    played = False
            if not played:
                _play_setup_tone_for_target(target_id, tone_path)
            time.sleep(max(0.2, PHASE1_INTER_BURST_SEC - SETUP_TONE_DURATION_SEC))
        time.sleep(PHASE1_POST_ROLL_SEC)
        try:
            recorder.wait(timeout=4.0)
        except Exception:
            recorder.terminate()
            try:
                recorder.wait(timeout=1.0)
            except Exception:
                recorder.kill()
    finally:
        _restore_probe_mix(bt_snapshot=bt_snapshot, sonos_snapshot=sonos_snapshot)
        try:
            os.remove(tone_path)
        except OSError:
            pass

    samples, sample_rate = _read_wav_mono_f32(str(wav_path))
    detections = _detect_sequence_onsets_ms(
        samples,
        sample_rate,
        send_events=send_events,
        template_waveform=template_waveform,
    )
    png_written = _write_sequence_debug_plot(
        samples,
        sample_rate,
        send_events=send_events,
        detections=detections,
        output_png=str(png_path),
    )

    pair_count = min(len(send_events), len(detections))
    pairs: List[Dict[str, Any]] = []
    for i in range(pair_count):
        send = send_events[i]
        heard_sec = float(detections[i]["onset_ms"]) / 1000.0
        pairs.append(
            {
                "index": i,
                "target_id": send["target_id"],
                "speaker": send["speaker"],
                "sent_sec": send["send_from_record_start_sec"],
                "heard_sec": round(heard_sec, 6),
                "approx_latency_sec": round(heard_sec - float(send["send_from_record_start_sec"]), 6),
            }
        )

    relative: List[Dict[str, Any]] = []
    for i in range(1, pair_count):
        prev_send = float(send_events[i - 1]["send_from_record_start_sec"])
        curr_send = float(send_events[i]["send_from_record_start_sec"])
        prev_heard = float(detections[i - 1]["onset_ms"]) / 1000.0
        curr_heard = float(detections[i]["onset_ms"]) / 1000.0
        send_spacing = curr_send - prev_send
        heard_spacing = curr_heard - prev_heard
        relative.append(
            {
                "from": send_events[i - 1]["speaker"],
                "to": send_events[i]["speaker"],
                "send_spacing_sec": round(send_spacing, 6),
                "heard_spacing_sec": round(heard_spacing, 6),
                "relative_delta_ms": round((heard_spacing - send_spacing) * 1000.0, 3),
            }
        )

    # Control-flow preview metrics (log-only in this phase; no actuation changes).
    transport_base_ms = float(get_transport_base_ms())
    control_state = read_control_state()
    outputs_state = control_state.get("outputs", {}) if isinstance(control_state, dict) else {}
    if not isinstance(outputs_state, dict):
        outputs_state = {}

    measured_map_ms: Dict[str, float] = {}
    for item in pairs:
        target_id = str(item.get("target_id", "")).strip()
        if not target_id:
            continue
        measured_ms = float(item.get("approx_latency_sec", 0.0)) * 1000.0
        measured_map_ms[target_id.upper()] = measured_ms

    measured_reference_ms = max(measured_map_ms.values()) if measured_map_ms else 0.0
    control_preview: List[Dict[str, Any]] = []
    for target_id in probe_order:
        key = str(target_id).upper()
        speaker = format_device_label(target_id)
        measured_latency_ms = measured_map_ms.get(key)
        baseline_delay_ms = (
            max(0.0, measured_reference_ms - float(measured_latency_ms))
            if measured_latency_ms is not None
            else None
        )
        output_cfg = outputs_state.get(key, {})
        if not isinstance(output_cfg, dict):
            output_cfg = {}
        current_delay_ms = float(output_cfg.get("delay_ms", transport_base_ms))
        slider_offset_ms = max(0.0, current_delay_ms - transport_base_ms)
        effective_delay_ms = (
            (float(baseline_delay_ms) + float(slider_offset_ms))
            if baseline_delay_ms is not None
            else None
        )
        item = {
            "target_id": target_id,
            "speaker": speaker,
            "measured_latency_ms": (round(float(measured_latency_ms), 3) if measured_latency_ms is not None else None),
            "baseline_delay_i_ms": (round(float(baseline_delay_ms), 3) if baseline_delay_ms is not None else None),
            "slider_offset_i_ms": round(float(slider_offset_ms), 3),
            "effective_delay_i_ms": (round(float(effective_delay_ms), 3) if effective_delay_ms is not None else None),
            "transport_base_ms": round(float(transport_base_ms), 3),
            "current_output_delay_ms": round(float(current_delay_ms), 3),
        }
        control_preview.append(item)
        log.info(
            "[PROBE_CTRL] speaker=%s measured_latency_ms=%s baseline_delay_i_ms=%s slider_offset_i_ms=%.3f effective_delay_i_ms=%s transport_base_ms=%.3f current_output_delay_ms=%.3f",
            speaker,
            ("%.3f" % float(measured_latency_ms)) if measured_latency_ms is not None else "na",
            ("%.3f" % float(baseline_delay_ms)) if baseline_delay_ms is not None else "na",
            float(slider_offset_ms),
            ("%.3f" % float(effective_delay_ms)) if effective_delay_ms is not None else "na",
            float(transport_base_ms),
            float(current_delay_ms),
        )

    result: Dict[str, Any] = {
        "reason": "startup_probe_phase1_complete",
        "setup_done": True,
        "data_only": True,
        "probe_order": [format_device_label(x) for x in probe_order],
        "send_events": send_events,
        "detections": detections,
        "paired": pairs,
        "relative": relative,
        "control_preview": control_preview,
        "artifacts": {
            "wav_path": str(wav_path),
            "png_path": png_written,
            "json_path": str(json_path),
        },
        "counts": {
            "expected_events": len(send_events),
            "detected_events": len(detections),
            "paired_events": pair_count,
        },
        "playback_mode": "phone_callback" if playback_callback is not None else "pi_local",
    }
    ok = (pair_count == len(send_events) and len(detections) >= len(send_events))
    if not ok:
        result["reason"] = "startup_probe_phase1_partial"
        result["setup_done"] = False
    with open(json_path, "w", encoding="ascii") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    return ok, result


def begin_frontend_probe_session(targets: List[str]) -> Tuple[bool, Dict[str, Any]]:
    _load_syncsonic_env()
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    probe_order = [str(x or "").strip() for x in targets if str(x or "").strip()]
    probe_order = list(dict.fromkeys(probe_order))
    if not probe_order:
        return False, {"reason": "no_targets"}

    has_sonos_target = any(is_sonos(target_id) for target_id in probe_order)
    inter_burst_sec = float(PHASE1_INTER_BURST_SEC)
    post_roll_sec = float(PHASE1_POST_ROLL_SEC)
    if has_sonos_target:
        inter_burst_sec = max(inter_burst_sec, float(FRONTEND_SONOS_INTER_BURST_SEC))
        post_roll_sec = max(post_roll_sec, float(FRONTEND_SONOS_POST_ROLL_SEC))
    total_duration = (
        PHASE1_PRE_ROLL_SEC
        + (len(probe_order) * (inter_burst_sec + FRONTEND_STEP_OVERHEAD_SEC))
        + post_roll_sec
        + 2.0
    )
    run_tag = int(time.time())
    wav_path = str(DEBUG_DIR / f"startup_probe_{run_tag}.wav")
    png_path = str(DEBUG_DIR / f"startup_probe_{run_tag}.png")
    json_path = str(DEBUG_DIR / f"startup_probe_{run_tag}.json")

    with _FRONTEND_PROBE_LOCK:
        global _FRONTEND_PROBE_SESSION
        if _FRONTEND_PROBE_SESSION is not None:
            return False, {"reason": "probe_session_already_active"}
        recorder = _record_measurement_for_duration(wav_path, total_duration)
        start_monotonic = time.monotonic()
        _FRONTEND_PROBE_SESSION = FrontendProbeSession(
            run_tag=run_tag,
            probe_order=probe_order,
            wav_path=wav_path,
            png_path=png_path,
            json_path=json_path,
            start_monotonic=start_monotonic,
            planned_total_duration_sec=float(total_duration),
            planned_stop_monotonic=float(start_monotonic + float(total_duration)),
            recorder=recorder,
            send_events=[],
        )
    log.info(
        "[PROBE] begin session run=%s targets=%s total_duration=%.2fs sonos_target=%s",
        run_tag,
        [format_device_label(x) for x in probe_order],
        float(total_duration),
        bool(has_sonos_target),
    )
    return True, {
        "run_tag": run_tag,
        "targets": [format_device_label(x) for x in probe_order],
        "planned_total_duration_sec": round(float(total_duration), 3),
        "sonos_target": bool(has_sonos_target),
    }


def mark_frontend_probe_step(target_id: str, speaker: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
    target = str(target_id or "").strip()
    if not target:
        return False, {"reason": "missing_target_id"}
    with _FRONTEND_PROBE_LOCK:
        global _FRONTEND_PROBE_SESSION
        session = _FRONTEND_PROBE_SESSION
        if session is None:
            return False, {"reason": "probe_session_not_active"}
        send_mono = time.monotonic()
        event = {
            "target_id": target,
            "speaker": str(speaker or format_device_label(target)),
            "send_monotonic": round(send_mono, 6),
            "send_from_record_start_sec": round(send_mono - session.start_monotonic, 6),
        }
        session.send_events.append(event)
        index = len(session.send_events)
    log.info("[PROBE_STEP] %s/%s target=%s", index, len(session.probe_order), format_device_label(target))
    return True, {"step_index": index, "event": event}


def finish_frontend_probe_session() -> Tuple[bool, Dict[str, Any]]:
    with _FRONTEND_PROBE_LOCK:
        global _FRONTEND_PROBE_SESSION
        session = _FRONTEND_PROBE_SESSION
        _FRONTEND_PROBE_SESSION = None
    if session is None:
        return False, {"reason": "probe_session_not_active"}

    try:
        remaining_sec = max(0.0, float(session.planned_stop_monotonic) - time.monotonic())
        wait_timeout_sec = max(4.0, remaining_sec + 1.5)
        session.recorder.wait(timeout=wait_timeout_sec)
    except Exception:
        session.recorder.terminate()
        try:
            session.recorder.wait(timeout=1.0)
        except Exception:
            session.recorder.kill()

    samples, sample_rate = _read_wav_mono_f32(session.wav_path)
    template_src = _find_probe_source_wav()
    template_waveform = (
        _load_probe_template_waveform(template_src, sample_rate)
        if template_src
        else _build_probe_waveform(sample_rate)
    )
    detections = _detect_sequence_onsets_ms(
        samples,
        sample_rate,
        send_events=session.send_events,
        template_waveform=template_waveform,
    )
    png_written = _write_sequence_debug_plot(
        samples,
        sample_rate,
        send_events=session.send_events,
        detections=detections,
        output_png=session.png_path,
    )

    pair_count = min(len(session.send_events), len(detections))
    pairs: List[Dict[str, Any]] = []
    for i in range(pair_count):
        send = session.send_events[i]
        heard_sec = float(detections[i]["onset_ms"]) / 1000.0
        pairs.append(
            {
                "index": i,
                "target_id": send["target_id"],
                "speaker": send["speaker"],
                "sent_sec": send["send_from_record_start_sec"],
                "heard_sec": round(heard_sec, 6),
                "approx_latency_sec": round(heard_sec - float(send["send_from_record_start_sec"]), 6),
            }
        )

    relative: List[Dict[str, Any]] = []
    for i in range(1, pair_count):
        prev_send = float(session.send_events[i - 1]["send_from_record_start_sec"])
        curr_send = float(session.send_events[i]["send_from_record_start_sec"])
        prev_heard = float(detections[i - 1]["onset_ms"]) / 1000.0
        curr_heard = float(detections[i]["onset_ms"]) / 1000.0
        send_spacing = curr_send - prev_send
        heard_spacing = curr_heard - prev_heard
        relative.append(
            {
                "from": session.send_events[i - 1]["speaker"],
                "to": session.send_events[i]["speaker"],
                "send_spacing_sec": round(send_spacing, 6),
                "heard_spacing_sec": round(heard_spacing, 6),
                "relative_delta_ms": round((heard_spacing - send_spacing) * 1000.0, 3),
            }
        )

    transport_base_ms = float(get_transport_base_ms())
    control_state = read_control_state()
    outputs_state = control_state.get("outputs", {}) if isinstance(control_state, dict) else {}
    if not isinstance(outputs_state, dict):
        outputs_state = {}
    measured_map_ms: Dict[str, float] = {}
    for item in pairs:
        measured_map_ms[str(item.get("target_id", "")).upper()] = float(item.get("approx_latency_sec", 0.0)) * 1000.0
    measured_reference_ms = max(measured_map_ms.values()) if measured_map_ms else 0.0
    control_preview: List[Dict[str, Any]] = []
    for target_id in session.probe_order:
        key = str(target_id).upper()
        measured_latency_ms = measured_map_ms.get(key)
        baseline_delay_ms = (
            max(0.0, measured_reference_ms - float(measured_latency_ms))
            if measured_latency_ms is not None
            else None
        )
        output_cfg = outputs_state.get(key, {})
        if not isinstance(output_cfg, dict):
            output_cfg = {}
        current_delay_ms = float(output_cfg.get("delay_ms", transport_base_ms))
        slider_offset_ms = max(0.0, current_delay_ms - transport_base_ms)
        effective_delay_ms = (
            (float(baseline_delay_ms) + float(slider_offset_ms))
            if baseline_delay_ms is not None
            else None
        )
        target_output_delay_ms = (
            float(current_delay_ms) + float(baseline_delay_ms)
            if baseline_delay_ms is not None
            else None
        )
        apply_supported = (baseline_delay_ms is not None) and (not is_sonos(target_id))
        control_preview.append(
            {
                "target_id": target_id,
                "speaker": format_device_label(target_id),
                "measured_latency_ms": (round(float(measured_latency_ms), 3) if measured_latency_ms is not None else None),
                "baseline_delay_i_ms": (round(float(baseline_delay_ms), 3) if baseline_delay_ms is not None else None),
                "slider_offset_i_ms": round(float(slider_offset_ms), 3),
                "effective_delay_i_ms": (round(float(effective_delay_ms), 3) if effective_delay_ms is not None else None),
                "transport_base_ms": round(float(transport_base_ms), 3),
                "current_output_delay_ms": round(float(current_delay_ms), 3),
                "target_output_delay_ms": (
                    round(float(target_output_delay_ms), 3) if target_output_delay_ms is not None else None
                ),
                "apply_supported": bool(apply_supported),
            }
        )

    manager = get_actuation_manager()
    applied: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for item in control_preview:
        log.info(
            "[PROBE_CTRL] speaker=%s measured_latency_ms=%s baseline_delay_i_ms=%s slider_offset_i_ms=%.3f effective_delay_i_ms=%s transport_base_ms=%.3f current_output_delay_ms=%.3f target_output_delay_ms=%s apply_supported=%s",
            item["speaker"],
            ("%.3f" % float(item["measured_latency_ms"])) if item["measured_latency_ms"] is not None else "na",
            ("%.3f" % float(item["baseline_delay_i_ms"])) if item["baseline_delay_i_ms"] is not None else "na",
            float(item["slider_offset_i_ms"]),
            ("%.3f" % float(item["effective_delay_i_ms"])) if item["effective_delay_i_ms"] is not None else "na",
            float(item["transport_base_ms"]),
            float(item["current_output_delay_ms"]),
            ("%.3f" % float(item["target_output_delay_ms"])) if item["target_output_delay_ms"] is not None else "na",
            item["apply_supported"],
        )
        if not item["apply_supported"]:
            continue
        target_delay_ms = float(item["target_output_delay_ms"])
        try:
            ok_apply, snapshot = manager.apply_control_target(
                str(item["target_id"]),
                delay_ms=target_delay_ms,
                rate_ppm=0.0,
                mode="manual:startup_probe",
            )
        except Exception as exc:
            ok_apply, snapshot = False, {"error": str(exc)}
        apply_entry = {
            "target_id": item["target_id"],
            "speaker": item["speaker"],
            "target_output_delay_ms": round(target_delay_ms, 3),
        }
        if ok_apply:
            if isinstance(snapshot, dict):
                apply_entry["applied_delay_ms"] = snapshot.get("delay_applied_ms")
            applied.append(apply_entry)
        else:
            if isinstance(snapshot, dict):
                apply_entry["error"] = snapshot.get("error")
            failed.append(apply_entry)

    result: Dict[str, Any] = {
        "reason": "startup_probe_frontend_complete",
        "setup_done": True,
        "data_only": False,
        "playback_mode": "frontend_orchestrated",
        "probe_order": [format_device_label(x) for x in session.probe_order],
        "send_events": session.send_events,
        "detections": detections,
        "paired": pairs,
        "relative": relative,
        "control_preview": control_preview,
        "actuation": {
            "applied": applied,
            "failed": failed,
        },
        "artifacts": {
            "wav_path": session.wav_path,
            "png_path": png_written,
            "json_path": session.json_path,
        },
        "counts": {
            "expected_events": len(session.send_events),
            "detected_events": len(detections),
            "paired_events": pair_count,
        },
    }
    ok = (
        pair_count == len(session.send_events)
        and len(detections) >= len(session.send_events)
        and not failed
    )
    if not ok:
        result["reason"] = "startup_probe_frontend_partial"
        result["setup_done"] = False
    with open(session.json_path, "w", encoding="ascii") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    return ok, result
