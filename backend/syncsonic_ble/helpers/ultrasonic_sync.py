#!/usr/bin/env python3
"""
SyncSonic Ultrasonic Auto-Sync Prototype – CLI.

Closed-loop speaker sync: inject 19 kHz bursts to speakers, record with USB mic,
detect arrival times, compute relative delay, adjust loopback latency.

Subcommands:
  dump-latency       Print effective loopback latency (Buffer + Sink) per speaker
  dump-sink-bounds   Print per-sink current/configured latency (pactl list sinks) for tracking PA limits
  play-burst MAC     Play 200 ms ultrasonic burst to one speaker
  record-detect      Record mic, detect two 19 kHz bursts, print t_A, t_B, delta_ms
  correct MAC MS     Rebuild loopback for MAC with new latency (ms); no decrease
  sync-once          Run one sync cycle: measure delta, apply one correction step
"""
from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from typing import Dict, List, Optional, Tuple

from syncsonic_ble.helpers.analyze_bursts_debug import detect_burst_onsets
from syncsonic_ble.helpers.actuation import get_actuation_manager
from syncsonic_ble.helpers.alignment_controller import get_alignment_controller

import numpy as np
from scipy import signal as scipy_signal

# Optional: use project logger if available
try:
    from syncsonic_ble.utils.logging_conf import get_logger
    log = get_logger(__name__)
except Exception:
    import logging
    log = logging.getLogger(__name__)
    if not log.handlers:
        logging.basicConfig(level=logging.INFO)
        log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment: PulseAudio socket (e.g. SyncSonic service)
# ---------------------------------------------------------------------------
SYNCSONIC_ENV = "/etc/default/syncsonic"
DEFAULT_PULSE_SERVER = "unix:/run/syncsonic/pulse/native"


def _load_syncsonic_env() -> None:
    if os.path.exists(SYNCSONIC_ENV):
        with open(SYNCSONIC_ENV, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export ") and "=" in line:
                    key, _, value = line[7:].partition("=")
                    os.environ[key] = value


def _pulse_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PULSE_SERVER", DEFAULT_PULSE_SERVER)
    return env


# ---------------------------------------------------------------------------
# Helpers: list speakers, sink names
# ---------------------------------------------------------------------------
def get_connected_speakers() -> List[str]:
    """Return list of connected Bluetooth speaker MACs (from PulseAudio sinks)."""
    result = subprocess.run(
        ["pactl", "list", "sinks", "short"],
        capture_output=True,
        text=True,
        env=_pulse_env(),
    )
    if result.returncode != 0:
        log.warning("pactl list sinks failed: %s", result.stderr)
        return []
    macs = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        sink_name = parts[1]
        mac_part: Optional[str] = None
        if "bluez_output." in sink_name:
            mac_part = sink_name.split(".")[1]
        elif "bluez_sink." in sink_name:
            mac_part = sink_name.split(".")[1]
        if mac_part:
            mac = mac_part.replace("_", ":").upper()
            if mac not in macs:
                macs.append(mac)
    return macs


def _mac_to_sink(mac: str) -> str:
    return f"bluez_sink.{mac.replace(':', '_')}.a2dp_sink"


# ---------------------------------------------------------------------------
# PulseAudio latency roles (do not mix when applying sync corrections)
# ---------------------------------------------------------------------------
# - Loopback (source) latency: We set it via latency_msec when creating the loopback
#   (load-module module-loopback). It is the buffer from virtual_out.monitor to the sink.
# - Sink latency: From pactl list sinks (current/configured). The Bluetooth sink's own
#   latency; we do not set it. PA may clamp our loopback request based on sink limits.
# - Effective latency: From pactl list sink-inputs (Buffer Latency + Sink Latency).
#   Total delay we use as "current" when computing corrections. Sync corrections must
#   use only delta_ms (the measured sync error), never sink shortfall or requested - configured.
# ---------------------------------------------------------------------------
# Sink latency bounds (what PA reports; min/max are not in the protocol)
# ---------------------------------------------------------------------------
def get_sink_latency_info_per_speaker() -> Dict[str, Dict[str, float]]:
    """
    Parse `pactl list sinks` for each bluez sink and return per-MAC latency info.

    PulseAudio does *not* expose explicit min_latency/max_latency in the protocol
    or in pactl output. This returns what we can read:

    - current_latency_ms: "Latency: X usec" (current measured sink latency)
    - configured_latency_ms: "configured Y usec" (target PA is using for the sink)

    When PA clamps a request (e.g. "adjusting to 39.51 ms"), that appears only in
    the PulseAudio daemon log (e.g. journalctl -u pulseaudio), not in pactl.
    To discover actual min/max you can:
    1) Watch PA logs for "Cannot set requested sink latency ... adjusting to X ms".
    2) Empirically: after apply_correction(), re-read effective latency and compare
       requested vs actual to infer when PA clamped.
    """
    result = subprocess.run(
        ["pactl", "list", "sinks"],
        capture_output=True,
        text=True,
        env=_pulse_env(),
    )
    if result.returncode != 0:
        return {}
    out: Dict[str, Dict[str, float]] = {}
    current_mac: Optional[str] = None
    for line in result.stdout.splitlines():
        if line.strip().startswith("Sink #"):
            current_mac = None
            continue
        if line.strip().startswith("Name:"):
            m = re.search(r"Name:\s*(\S+)", line)
            if m:
                name = m.group(1)
                if "bluez_sink." in name and ".a2dp_sink" in name:
                    mac_part = name.split("bluez_sink.")[1].split(".")[0]
                    current_mac = mac_part.replace("_", ":")
                    out[current_mac] = {}
                else:
                    current_mac = None
            continue
        if current_mac is not None and current_mac in out and "Latency:" in line:
            # "Latency: 44675 usec, configured 39512 usec"
            cur_m = re.search(r"Latency:\s*([\d.]+)\s*usec", line)
            cfg_m = re.search(r"configured\s+([\d.]+)\s*usec", line)
            if cur_m:
                out[current_mac]["current_latency_ms"] = float(cur_m.group(1)) / 1000.0
            if cfg_m:
                out[current_mac]["configured_latency_ms"] = float(cfg_m.group(1)) / 1000.0
            current_mac = None
    return out


# ---------------------------------------------------------------------------
# 1) Dump effective loopback latency (Buffer + Sink) per speaker
# ---------------------------------------------------------------------------
def _get_sink_index_to_name(env: Dict[str, str]) -> Dict[int, str]:
    """Parse pactl list sinks and return mapping sink_index -> sink name (e.g. bluez_sink.XX.a2dp_sink)."""
    result = subprocess.run(
        ["pactl", "list", "sinks"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        return {}
    index_to_name: Dict[int, str] = {}
    current_index: Optional[int] = None
    for line in result.stdout.splitlines():
        m = re.match(r"Sink #(\d+)\s*$", line.strip())
        if m:
            current_index = int(m.group(1))
            continue
        if current_index is not None and line.strip().startswith("Name:"):
            name_m = re.search(r"Name:\s*(\S+)", line)
            if name_m:
                index_to_name[current_index] = name_m.group(1)
            current_index = None
    return index_to_name


def get_effective_loopback_latency_per_speaker() -> Dict[str, float]:
    """
    From pactl list sink-inputs: for each sink-input that feeds a bluez sink,
    compute effective latency = Buffer Latency + Sink Latency (usec -> ms).
    Sink-inputs may report Sink as an index (e.g. "Sink: 2"); we resolve via pactl list sinks.
    Returns dict mac -> L_eff_ms.
    """
    env = _pulse_env()
    log.info("[pactl] PULSE_SERVER=%s", env.get("PULSE_SERVER", "<not set>"))
    index_to_name = _get_sink_index_to_name(env)
    log.info("[pactl] sink index -> name: %s", index_to_name)

    result = subprocess.run(
        ["pactl", "list", "sink-inputs"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        log.warning("pactl list sink-inputs failed: returncode=%s stderr=%r", result.returncode, result.stderr)
        return {}

    raw = result.stdout
    log.info("[pactl] pactl list sink-inputs raw output (length=%d):\n%s", len(raw), raw if raw else "(empty)")

    out: Dict[str, float] = {}
    block = []
    for line in result.stdout.splitlines():
        if line.startswith("Sink Input #"):
            block = []
            block.append(line)
            continue
        if line.strip() == "" and block:
            mac, L_ms = _parse_sink_input_block(block, index_to_name)
            log.info("[pactl] parsed block (%d lines): mac=%r L_ms=%r", len(block), mac, L_ms)
            if mac is not None and L_ms is not None:
                out[mac] = L_ms
            block = []
        else:
            block.append(line)
    if block:
        mac, L_ms = _parse_sink_input_block(block, index_to_name)
        log.info("[pactl] parsed final block (%d lines): mac=%r L_ms=%r", len(block), mac, L_ms)
        if mac is not None and L_ms is not None:
            out[mac] = L_ms
    log.info("[pactl] get_effective_loopback_latency_per_speaker result: %s", out)
    return out


def _parse_sink_input_block(
    lines: List[str],
    sink_index_to_name: Optional[Dict[int, str]] = None,
) -> Tuple[Optional[str], Optional[float]]:
    """Parse one sink-input block. Resolve Sink index to name when pactl reports e.g. 'Sink: 2'."""
    sink_index_to_name = sink_index_to_name or {}
    sink_ref = None  # raw value from "Sink: X" (index or name)
    buffer_usec = None
    sink_usec = None
    for line in lines:
        if "Sink:" in line:
            m = re.search(r"Sink:\s*(\S+)", line)
            if m:
                sink_ref = m.group(1)
        if "Buffer Latency:" in line:
            m = re.search(r"Buffer Latency:\s*([\d.]+)\s*usec", line)
            if m:
                buffer_usec = float(m.group(1))
        if "Sink Latency:" in line:
            m = re.search(r"Sink Latency:\s*([\d.]+)\s*usec", line)
            if m:
                sink_usec = float(m.group(1))
    # Resolve sink index to name (pactl often reports "Sink: 2" not "Sink: bluez_sink.XX.a2dp_sink")
    sink_name = sink_ref
    if sink_ref and sink_ref.isdigit():
        idx = int(sink_ref)
        sink_name = sink_index_to_name.get(idx, sink_ref)
    if not sink_name or "bluez_sink." not in sink_name or ".a2dp_sink" not in sink_name:
        log.debug("[pactl] skip block: sink_ref=%r resolved sink_name=%r (not bluez a2dp)", sink_ref, sink_name)
        return None, None
    mac_part = sink_name.split("bluez_sink.")[1].split(".")[0]
    mac = mac_part.replace("_", ":")
    total_usec = (buffer_usec or 0) + (sink_usec or 0)
    if buffer_usec is None and sink_usec is None:
        log.info("[pactl] skip block: bluez sink %r has no Buffer Latency or Sink Latency (sink_name=%r)", mac, sink_name)
        return None, None
    L_ms = total_usec / 1000.0
    return mac, L_ms


def cmd_dump_latency() -> None:
    """Print effective loopback latency (Buffer + Sink from sink-inputs) per speaker."""
    _load_syncsonic_env()
    latencies = get_effective_loopback_latency_per_speaker()
    if not latencies:
        print("No loopback sink-inputs found (no speakers with active loopback?). Check logs for pactl list sink-inputs raw output.")
        return
    print("Effective loopback latency (Buffer + Sink) per speaker:")
    for mac, L_ms in sorted(latencies.items()):
        print(f"  {mac}: {L_ms:.2f} ms")


def cmd_dump_sink_bounds() -> None:
    """Print per-speaker sink latency from pactl list sinks (current + configured). Use for tracking PA limits."""
    _load_syncsonic_env()
    info = get_sink_latency_info_per_speaker()
    if not info:
        print("No bluez sinks found.")
        return
    print("Sink latency (from pactl list sinks) per speaker:")
    for mac in sorted(info.keys()):
        d = info[mac]
        cur = d.get("current_latency_ms")
        cfg = d.get("configured_latency_ms")
        cur_s = f"{cur:.2f} ms" if cur is not None else "?"
        cfg_s = f"{cfg:.2f} ms" if cfg is not None else "?"
        print(f"  {mac}:  current={cur_s}  configured={cfg_s}")
    print("\nNote: PA does not expose min/max in pactl. To see when PA clamps, check PulseAudio logs, e.g.:")
    print("  journalctl -u pulseaudio -f | grep -i latency")
    print("Or use apply_correction_with_feedback() and compare requested vs actual_effective_ms.")


# ---------------------------------------------------------------------------
# 2) Generate and play ultrasonic burst to one speaker
# ---------------------------------------------------------------------------
BURST_FREQ_HZ = 19000
BURST_DURATION_SEC = 0.2
BURST_SAMPLE_RATE = 48000


def _generate_ultrasonic_wav(path: str) -> None:
    """Write 200 ms of 19 kHz sine (with short fades) to WAV at path."""
    n = int(BURST_SAMPLE_RATE * BURST_DURATION_SEC)
    fade = int(0.01 * BURST_SAMPLE_RATE)  # 10 ms fade
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(BURST_SAMPLE_RATE)
        for i in range(n):
            t = i / BURST_SAMPLE_RATE
            x = math.sin(2 * math.pi * BURST_FREQ_HZ * t)
            if i < fade:
                x *= i / fade
            elif i >= n - fade:
                x *= (n - 1 - i) / fade
            sample = max(-32767, min(32767, int(x * 32767 * 0.8)))
            wav.writeframes(struct.pack("<h", sample))
    return


def play_burst_to_speaker(mac: str) -> bool:
    """Play 200 ms ultrasonic burst to the given speaker only. Returns True on success."""
    sink = _mac_to_sink(mac)
    fd, path = tempfile.mkstemp(suffix=".wav")
    try:
        os.close(fd)
        _generate_ultrasonic_wav(path)
        result = subprocess.run(
            ["paplay", "-d", sink, path],
            capture_output=True,
            text=True,
            env=_pulse_env(),
        )
        if result.returncode != 0:
            log.warning("paplay failed: %s", result.stderr)
            return False
        return True
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def cmd_play_burst(mac: str) -> None:
    """CLI: play ultrasonic burst to MAC."""
    _load_syncsonic_env()
    if play_burst_to_speaker(mac):
        print(f"Played 200 ms ultrasonic burst to {mac}")
    else:
        print(f"Failed to play burst to {mac}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# 3) Record mic and detect two 19 kHz bursts (Goertzel)
# ---------------------------------------------------------------------------
RECORD_SAMPLE_RATE = 48000
RECORD_CHANNELS = 1
RECORD_FORMAT = "S16_LE"
# Burst onset detection: bandpass 19 kHz -> short-time energy -> contiguous segments -> onset
BURST_BANDPASS_LOW_HZ = 18000
BURST_BANDPASS_HIGH_HZ = 20000
BURST_ENERGY_WINDOW_MS = 10
BURST_THRESHOLD_FRAC = 0.1  # fraction of max 19 kHz energy; 0.1 catches weaker first burst
BURST_MIN_DURATION_MS = 50
BURST_MAX_GAP_MS = 100
BURST_ONSET_REFINE_FRAC = 0.2


def _read_wav_s16_mono(path: str) -> Tuple[List[float], int]:
    """Return (samples as float in [-1,1], sample_rate)."""
    with wave.open(path, "rb") as wav:
        sr = wav.getframerate()
        nch = wav.getnchannels()
        n = wav.getnframes()
        raw = wav.readframes(n)
    if nch == 2:
        vals = [
            struct.unpack_from("<h", raw, i)[0] / 32768.0
            for i in range(0, len(raw), 4)
        ]
        return vals, sr
    count = len(raw) // 2
    vals = struct.unpack(f"<{count}h", raw[: count * 2])
    return [v / 32768.0 for v in vals], sr


def _bandpass_19k(x: np.ndarray, sr: float, order: int = 4) -> np.ndarray:
    """Zero-phase bandpass 18–20 kHz to isolate burst energy."""
    nyq = sr / 2.0
    low = max(0.01, BURST_BANDPASS_LOW_HZ / nyq)
    high = min(0.99, BURST_BANDPASS_HIGH_HZ / nyq)
    b, a = scipy_signal.butter(order, [low, high], btype="band")
    return scipy_signal.filtfilt(b, a, x)


def _short_time_energy(
    x: np.ndarray, window_samples: int, hop: int, sr: float
) -> Tuple[np.ndarray, np.ndarray]:
    """RMS energy in sliding windows. Returns (time_sec, energy)."""
    n = len(x)
    num_win = max(0, (n - window_samples) // hop + 1)
    t_sec = (np.arange(num_win) * hop + window_samples / 2) / sr
    energy = np.zeros(num_win)
    for i in range(num_win):
        start = i * hop
        end = start + window_samples
        if end > n:
            break
        energy[i] = np.sqrt(np.mean(x[start:end] ** 2))
    return t_sec[: len(energy)], energy


def _find_contiguous_burst_segments(
    t_sec: np.ndarray,
    above: np.ndarray,
    min_duration_sec: float,
    max_gap_sec: float,
) -> List[Tuple[float, float]]:
    """Find contiguous above-threshold segments; merge if gap <= max_gap_sec. Returns (start_sec, end_sec)."""
    if len(t_sec) == 0 or len(above) == 0:
        return []
    segments: List[Tuple[float, float]] = []
    in_segment = False
    seg_start = 0.0
    for i in range(len(above)):
        if above[i] and not in_segment:
            seg_start = float(t_sec[i])
            in_segment = True
        elif not above[i] and in_segment:
            seg_end = float(t_sec[i - 1])
            in_segment = False
            if seg_end - seg_start >= min_duration_sec:
                if segments and (seg_start - segments[-1][1]) <= max_gap_sec:
                    segments[-1] = (segments[-1][0], seg_end)
                else:
                    segments.append((seg_start, seg_end))
    if in_segment and (float(t_sec[-1]) - seg_start) >= min_duration_sec:
        if segments and (seg_start - segments[-1][1]) <= max_gap_sec:
            segments[-1] = (segments[-1][0], float(t_sec[-1]))
        else:
            segments.append((seg_start, float(t_sec[-1])))
    return segments


def _refine_burst_onset(
    t_sec: np.ndarray,
    energy: np.ndarray,
    segment: Tuple[float, float],
    frac: float = BURST_ONSET_REFINE_FRAC,
) -> float:
    """Within segment, first time energy crosses above (min + frac*(max-min))."""
    seg_start, seg_end = segment
    mask = (t_sec >= seg_start - 0.1) & (t_sec <= seg_end + 0.1)
    t = t_sec[mask]
    e = energy[mask]
    if len(e) == 0:
        return seg_start
    lo, hi = float(np.min(e)), float(np.max(e))
    thresh = lo + frac * (hi - lo)
    for i in range(len(e)):
        if e[i] >= thresh:
            return float(t[i])
    return seg_start


def _generate_spectrogram_with_markers(
    wav_path: str,
    t1_sec: Optional[float],
    t2_sec: Optional[float],
    output_path: str,
    sample_rate: int = RECORD_SAMPLE_RATE,
) -> None:
    """Generate a spectrogram PNG; optional vertical lines at t1_sec, t2_sec when provided."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log.warning("matplotlib not installed; skipping spectrogram.")
        return
    samples, sr = _read_wav_s16_mono(wav_path)
    if sr != sample_rate:
        log.warning("Spectrogram: sample rate %s != %s", sr, sample_rate)
    x = np.array(samples, dtype=np.float64)
    nperseg = min(2048, len(x) // 4)
    if nperseg < 64:
        log.warning("Recording too short for spectrogram.")
        return
    f, t, Sxx = scipy_signal.spectrogram(x, sr, nperseg=nperseg)
    Sxx_db = 10 * np.log10(Sxx + 1e-12)
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.pcolormesh(t, f, Sxx_db, shading="auto", cmap="viridis")
    if t1_sec is not None:
        ax.axvline(x=t1_sec, color="cyan", linewidth=1.5, label="t1 (first peak)")
    if t2_sec is not None:
        ax.axvline(x=t2_sec, color="orange", linewidth=1.5, label="t2 (second peak)")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Frequency (Hz)")
    ax.set_title("Recording spectrogram with detected 19 kHz burst times" if (t1_sec is not None and t2_sec is not None) else "Recording spectrogram (no peaks detected)")
    if t1_sec is not None or t2_sec is not None:
        ax.legend(loc="upper right")
    ax.set_ylim(0, min(24000, f.max()))
    try:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        fig.savefig(output_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        log.info("Wrote spectrogram to %s", output_path)
        # Grep-friendly line so you can quickly find the image path in logs
        log.info("SYNC_DEBUG_IMAGE=%s", os.path.abspath(output_path))
    except OSError as e:
        log.warning("Could not write spectrogram: %s", e)
    plt.close(fig)


def detect_two_burst_times(
    wav_path: str,
    freq_hz: float = BURST_FREQ_HZ,
    sample_rate: int = RECORD_SAMPLE_RATE,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Detect onset times of the first two 19 kHz bursts (bandpass -> short-time energy
    -> contiguous segments -> refined onset). Returns (t1_sec, t2_sec) from start of
    recording, or (None, None) on failure.
    """
    samples, sr = _read_wav_s16_mono(wav_path)
    if sr != sample_rate:
        log.warning("Resampling not implemented; got %s Hz", sr)
    x = np.array(samples, dtype=np.float64)

    # 1) Bandpass around 19 kHz
    filtered = _bandpass_19k(x, float(sr))

    # 2) Short-time energy (RMS in small windows)
    win_samples = max(64, int(sr * BURST_ENERGY_WINDOW_MS / 1000))
    hop = max(1, win_samples // 2)
    t_sec, energy = _short_time_energy(filtered, win_samples, hop, float(sr))
    if len(t_sec) == 0:
        return None, None

    # 3) Threshold: burst = energy above fraction of max
    emax = float(np.max(energy))
    threshold = max(emax * BURST_THRESHOLD_FRAC, 1e-9)
    above = energy >= threshold

    # 4) Contiguous segments = bursts
    min_dur_sec = BURST_MIN_DURATION_MS / 1000.0
    max_gap_sec = BURST_MAX_GAP_MS / 1000.0
    segments = _find_contiguous_burst_segments(t_sec, above, min_dur_sec, max_gap_sec)

    if len(segments) < 2:
        log.warning("Found %d burst segments (need 2); threshold=%.2e", len(segments), threshold)
        return None, None

    t1 = _refine_burst_onset(t_sec, energy, segments[0])
    t2 = _refine_burst_onset(t_sec, energy, segments[1])
    return t1, t2


def cmd_record_detect(
    record_sec: float = 2.0,
    device: Optional[str] = None,
) -> None:
    """Record from default (or given) mic, detect two bursts, print t_A, t_B, delta_ms."""
    _load_syncsonic_env()
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        cmd = [
            "arecord",
            "-f", RECORD_FORMAT,
            "-r", str(RECORD_SAMPLE_RATE),
            "-c", str(RECORD_CHANNELS),
            "-d", str(int(record_sec)),
            "-q",
            wav_path,
        ]
        if device:
            cmd.extend(["-D", device])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("arecord failed:", result.stderr, file=sys.stderr)
            sys.exit(1)
        t1, t2, _ = detect_burst_onsets(wav_path, sr=RECORD_SAMPLE_RATE)
        if t1 is None:
            print("Could not detect two bursts.")
            sys.exit(1)
        delta_ms = (t2 - t1) * 1000.0
        print(f"t1={t1:.4f} s  t2={t2:.4f} s  delta_ms={delta_ms:.2f}")
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 4) Correction: rebuild loopback with new latency (no decrease)
# ---------------------------------------------------------------------------
def unload_loopback_for_sink(sink_name: str) -> bool:
    """Unload the loopback module that feeds the given sink. Returns True if found and unloaded."""
    result = subprocess.run(
        ["pactl", "list", "short", "modules"],
        capture_output=True,
        text=True,
        env=_pulse_env(),
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and "module-loopback" in parts[1] and sink_name in line:
            module_id = parts[0]
            subprocess.run(
                ["pactl", "unload-module", module_id],
                capture_output=True,
                env=_pulse_env(),
            )
            return True
    return False


def create_loopback_for_sink(sink_name: str, latency_ms: int) -> bool:
    """Create loopback virtual_out.monitor -> sink_name with given latency_ms."""
    result = subprocess.run([
        "pactl", "load-module", "module-loopback",
        "source=virtual_out.monitor",
        f"sink={sink_name}",
        "source_dont_move=true",
        f"latency_msec={latency_ms}",
    ], capture_output=True, text=True, env=_pulse_env())
    if result.returncode != 0:
        log.warning("load-module loopback failed: %s", result.stderr)
        return False
    return True


def apply_correction(mac: str, new_latency_ms: int, allow_decrease: bool = False) -> bool:
    """
    Apply an explicit commanded delay for MAC.

    The commanded delay is now tracked in the actuation manager rather than
    derived from the sink's effective latency report.
    """
    manager = get_actuation_manager()
    current = manager.get_commanded_delay(mac)
    if current is not None and not allow_decrease and new_latency_ms < int(current):
        log.info("Skipping decrease: current %.1f ms >= requested %d ms", current, new_latency_ms)
        return False
    ok, _snapshot = manager.apply_fallback_delay(mac, float(new_latency_ms), mode="cli_correct")
    return ok


def apply_correction_with_feedback(
    mac: str, new_latency_ms: int, allow_decrease: bool = False
) -> Tuple[bool, int, Optional[float]]:
    """
    Like apply_correction, but after applying we re-read effective latency so
    you can track requested vs actual (to infer PA min/max clamping).

    Returns (did_apply, requested_ms, actual_effective_ms_after).
    did_apply is True only if we rebuilt the loopback; actual is None if we didn't apply.
    """
    requested = new_latency_ms
    ok = apply_correction(mac, new_latency_ms, allow_decrease=allow_decrease)
    if not ok:
        return False, requested, None
    time.sleep(0.5)
    latencies = get_effective_loopback_latency_per_speaker()
    actual = latencies.get(mac)
    return True, requested, actual


def cmd_correct(mac: str, latency_ms: int, allow_decrease: bool = False) -> None:
    """CLI: set loopback latency for MAC to latency_ms (no decrease by default)."""
    _load_syncsonic_env()
    if apply_correction(mac, latency_ms, allow_decrease=allow_decrease):
        print(f"Loopback for {mac} set to {latency_ms} ms")
    else:
        current = get_effective_loopback_latency_per_speaker().get(mac)
        if current is not None and not allow_decrease and current >= latency_ms:
            print(f"Already at or above {latency_ms} ms (current {current:.0f} ms); no change.")
        else:
            print(f"Failed to apply correction for {mac}", file=sys.stderr)
            sys.exit(1)


# ---------------------------------------------------------------------------
# 5) Sync once: relative timing (Option B), then correct using current loopback
# ---------------------------------------------------------------------------
RECORD_DURATION_SYNC = 8
SETTLE_SEC = 0.2
# Must be large enough (e.g. 5s) so the two 19 kHz onsets don't overlap in the recording.
WAIT_BETWEEN_BURSTS_SEC = 5.0
MAX_SPACING_ERROR_SEC = 0.75
# Debug output paths (for inspection when running under PulseAudio / service).
# On the Pi this is typically /tmp/syncsonic_debug. See DEBUG_OUTPUT.md for how to download via SFTP.
SYNCSONIC_DEBUG_DIR = os.path.join(tempfile.gettempdir(), "syncsonic_debug")
SYNCSONIC_LAST_WAV = os.path.join(SYNCSONIC_DEBUG_DIR, "last_recording.wav")
SYNCSONIC_SPECTROGRAM_PNG = os.path.join(SYNCSONIC_DEBUG_DIR, "spectrogram.png")
SYNCSONIC_META_TXT = os.path.join(SYNCSONIC_DEBUG_DIR, "last_sync_meta.txt")


def _build_auto_sync_plan(
    manager,
    controller,
    reference_mac: str,
    target_mac: str,
    measured_offset_ms: float,
) -> Dict[str, object]:
    reference_delay_ms = manager.get_commanded_delay(reference_mac)
    target_delay_ms = manager.get_commanded_delay(target_mac)
    plan = controller.plan_relative_correction(
        reference_mac=reference_mac,
        target_mac=target_mac,
        measured_offset_ms=measured_offset_ms,
        reference_delay_ms=reference_delay_ms,
        target_delay_ms=target_delay_ms,
    )
    manager.record_measurement(
        reference_mac,
        target_mac,
        raw_offset_ms=plan.measured_offset_ms,
        filtered_offset_ms=plan.filtered_offset_ms,
        ratio_ppm=plan.target_ratio_ppm,
        mode=f"auto_sync_{plan.controller_mode}",
    )
    return plan.as_dict()


def _apply_auto_sync_plan(manager, controller, plan: Dict[str, object]) -> Dict[str, object]:
    details = dict(plan)
    action = str(details.get("action", "rate_only"))

    if action == "rate_only":
        target_mac = str(details["target_mac"])
        rate_ppm = float(details.get("target_ratio_ppm", 0.0))
        target_delay_ms = manager.get_commanded_delay(target_mac)
        ok, _snapshot = manager.apply_control_target(
            target_mac,
            delay_ms=target_delay_ms,
            rate_ppm=rate_ppm,
            mode=f"auto_sync_{details.get('controller_mode', 'lock')}",
        )
        details["applied"] = ok
        details["states"] = manager.get_status_snapshot()
        details["controller_states"] = controller.get_state_snapshot()
        return details

    adjusted_mac = str(details["adjusted_mac"])
    target_mac = str(details["target_mac"])
    target_delay_ms = float(details["target_delay_ms"])
    target_rate_ppm = float(details.get("target_ratio_ppm", 0.0))
    relock = bool(details.get("relock", False))
    manager.note_auto_sync_action(adjusted_mac, relock=relock)
    adjusted_rate_ppm = target_rate_ppm if adjusted_mac == target_mac else 0.0
    ok, _snapshot = manager.apply_control_target(
        adjusted_mac,
        delay_ms=target_delay_ms,
        rate_ppm=adjusted_rate_ppm,
        mode="auto_sync_relock" if relock else "auto_sync_slew",
    )
    if adjusted_mac != target_mac:
        manager.record_rate_target(
            target_mac,
            target_rate_ppm,
            mode=f"auto_sync_{details.get('controller_mode', 'acquire')}",
        )
    details["applied"] = ok
    details["states"] = manager.get_status_snapshot()
    details["controller_states"] = controller.get_state_snapshot()
    return details


def sync_once(
    record_sec: float = RECORD_DURATION_SYNC,
    dry_run: bool = False,
) -> Tuple[bool, Dict[str, object]]:
    """
    Run one sync cycle for exactly two speakers using relative timing (Option B):
    1) Read and log effective loopback latency per speaker (not used as baseline).
    2) Start recording; play burst to A, note T_send_A; wait WAIT_BETWEEN_BURSTS_SEC;
       play burst to B, note T_send_B; wait for B to be heard.
    3) Detect two 19 kHz onsets in recording (first = A, second = B; spacing ensures no overlap).
    4) delta_ms = (t2 - t1)*1000 - (T_send_B - T_send_A)*1000 (no global clock alignment needed).
    5) Feed the measured relative error into the actuation manager, which owns the
       commanded delay state and applies either a bounded slew or a re-lock jump.
    Returns True if correction was applied (or not needed).
    """
    _load_syncsonic_env()
    macs = get_connected_speakers()
    manager = get_actuation_manager()
    controller = get_alignment_controller()
    if len(macs) < 2:
        log.warning("Need at least 2 speakers; found %s", macs)
        return False, {"reason": "need_two_speakers", "connected": macs}
    mac_a, mac_b = macs[0], macs[1]
    manager.ensure_output(mac_a)
    manager.ensure_output(mac_b)

    latencies = get_effective_loopback_latency_per_speaker()
    print("Effective loopback latency (ms):", {m: latencies.get(m, "?") for m in [mac_a, mac_b]})

    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        proc = subprocess.Popen(
            [
                "arecord", "-f", RECORD_FORMAT,
                "-r", str(RECORD_SAMPLE_RATE), "-c", str(RECORD_CHANNELS),
                "-d", str(int(record_sec)), "-q", wav_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        time.sleep(SETTLE_SEC)
        T_send_A = time.monotonic()
        play_burst_to_speaker(mac_a)
        time.sleep(WAIT_BETWEEN_BURSTS_SEC)
        T_send_B = time.monotonic()
        play_burst_to_speaker(mac_b)
        time.sleep(2.0)
        proc.wait(timeout=record_sec + 2)
        if proc.returncode != 0:
            log.warning("arecord stderr: %s", proc.stderr.read() if proc.stderr else "")
    finally:
        pass

    t1, t2, _ = detect_burst_onsets(wav_path, sr=RECORD_SAMPLE_RATE)
    # Save recording and spectrogram for inspection (works while PulseAudio is running)
    os.makedirs(SYNCSONIC_DEBUG_DIR, exist_ok=True)
    shutil.copy(wav_path, SYNCSONIC_LAST_WAV)
    try:
        os.unlink(wav_path)
    except OSError:
        pass

    if t1 is None:
        with open(SYNCSONIC_META_TXT, "w") as f:
            f.write("t1=None\n t2=None\n peaks_detected=0\n mac_a=%s\n mac_b=%s\n" % (mac_a, mac_b))
        # Always write spectrogram so you can inspect why detection failed
        _generate_spectrogram_with_markers(SYNCSONIC_LAST_WAV, None, None, SYNCSONIC_SPECTROGRAM_PNG)
        print("Could not detect two bursts; no correction applied.")
        print("Debug output: %s (WAV), %s (meta)" % (SYNCSONIC_LAST_WAV, SYNCSONIC_META_TXT))
        print("[SYNC_DEBUG] Spectrogram (open or scp this): %s" % os.path.abspath(SYNCSONIC_SPECTROGRAM_PNG))
        log.info("SYNC_DEBUG_IMAGE=%s", os.path.abspath(SYNCSONIC_SPECTROGRAM_PNG))
        return False, {"reason": "burst_detection_failed", "states": manager.get_status_snapshot()}
    # Option B: delta from relative timing only (no T_record_start / clock alignment)
    # delta_ms > 0 means B arrived later than A would imply -> B is slower -> add delay to A
    # delta_ms < 0 means A is slower -> add delay to B
    send_spacing_sec = T_send_B - T_send_A
    peak_spacing_sec = t2 - t1
    delta_ms = (peak_spacing_sec - send_spacing_sec) * 1000.0
    # Write metadata and spectrogram for inspection
    with open(SYNCSONIC_META_TXT, "w") as f:
        f.write(
            "t1=%.4f\n t2=%.4f\n peak_spacing_sec=%.4f\n send_spacing_sec=%.4f\n delta_ms=%.2f\n mac_a=%s\n mac_b=%s\n"
            % (t1, t2, peak_spacing_sec, send_spacing_sec, delta_ms, mac_a, mac_b)
        )
    _generate_spectrogram_with_markers(SYNCSONIC_LAST_WAV, t1, t2, SYNCSONIC_SPECTROGRAM_PNG)
    print("Debug output: %s (WAV) | %s (spectrogram) | %s (meta)" % (SYNCSONIC_LAST_WAV, SYNCSONIC_SPECTROGRAM_PNG, SYNCSONIC_META_TXT))
    print("[SYNC_DEBUG] Spectrogram (open or scp this): %s" % os.path.abspath(SYNCSONIC_SPECTROGRAM_PNG))
    print(f"Peak spacing (t2-t1)={peak_spacing_sec*1000:.1f} ms  send spacing={send_spacing_sec*1000:.1f} ms  delta_ms={delta_ms:.1f} (B - A)")
    # Sanity: peak spacing should be close to send spacing (~5s) if we detected the two bursts correctly
    print(f"[sanity] Expected peak spacing ≈ send spacing (={send_spacing_sec:.2f}s); got {peak_spacing_sec:.2f}s → delta_ms={delta_ms:.1f}")
    log.info("[sanity] send_spacing_sec=%.2f peak_spacing_sec=%.2f delta_ms=%.1f", send_spacing_sec, peak_spacing_sec, delta_ms)
    spacing_error_sec = abs(peak_spacing_sec - send_spacing_sec)
    if spacing_error_sec > MAX_SPACING_ERROR_SEC:
        log.warning(
            "Rejecting sync measurement: spacing sanity failed (expected %.2fs, got %.2fs, error %.2fs)",
            send_spacing_sec,
            peak_spacing_sec,
            spacing_error_sec,
        )
        return False, {
            "reason": "spacing_sanity_failed",
            "offset_ms": delta_ms,
            "peak_spacing_sec": peak_spacing_sec,
            "send_spacing_sec": send_spacing_sec,
            "states": manager.get_status_snapshot(),
            "controller_states": controller.get_state_snapshot(),
        }

    if abs(delta_ms) < 0.5:
        print("Already in sync (|delta| < 0.5 ms).")
        plan = _build_auto_sync_plan(manager, controller, mac_a, mac_b, delta_ms)
        details = _apply_auto_sync_plan(manager, controller, plan)
        return True, {
            **details,
            "action": "rate_only",
        }

    if dry_run:
        print("[dry-run] Would apply correction.")
        plan = _build_auto_sync_plan(manager, controller, mac_a, mac_b, delta_ms)
        details = dict(plan)
        details["applied"] = False
        details["states"] = manager.get_status_snapshot()
        details["controller_states"] = controller.get_state_snapshot()
        return True, {
            **details,
            "action": "dry_run",
        }
    plan = _build_auto_sync_plan(manager, controller, mac_a, mac_b, delta_ms)
    details = _apply_auto_sync_plan(manager, controller, plan)
    return bool(details.get("applied")), details


def cmd_sync_once(
    record_sec: float = RECORD_DURATION_SYNC,
    dry_run: bool = False,
) -> None:
    """CLI: run one sync-once cycle."""
    ok, _details = sync_once(record_sec=record_sec, dry_run=dry_run)
    if not ok:
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    _load_syncsonic_env()
    parser = argparse.ArgumentParser(
        description="SyncSonic ultrasonic auto-sync prototype: dump latency, play burst, record-detect, correct, sync-once.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("dump-latency", help="Print effective loopback latency per speaker")
    sub.add_parser("dump-sink-bounds", help="Print per-sink current/configured latency from pactl list sinks (for tracking PA limits)")

    p_burst = sub.add_parser("play-burst", help="Play 200 ms ultrasonic burst to one speaker")
    p_burst.add_argument("mac", help="Speaker MAC (e.g. AA:BB:CC:DD:EE:FF)")

    p_rec = sub.add_parser("record-detect", help="Record mic, detect two 19 kHz bursts, print t_A, t_B, delta_ms")
    p_rec.add_argument("--seconds", type=float, default=2.0, help="Record duration (default 2)")
    p_rec.add_argument("--device", type=str, default=None, help="arecord -D device")

    p_correct = sub.add_parser("correct", help="Rebuild loopback for MAC with new latency (ms); no decrease by default")
    p_correct.add_argument("mac", help="Speaker MAC")
    p_correct.add_argument("ms", type=int, help="New latency in ms")
    p_correct.add_argument("--allow-decrease", action="store_true", help="Allow decreasing latency")

    p_sync = sub.add_parser("sync-once", help="Run one sync cycle: measure delta, apply one correction")
    p_sync.add_argument("--seconds", type=float, default=RECORD_DURATION_SYNC, help="Record duration")
    p_sync.add_argument("--dry-run", action="store_true", help="Do not apply correction")

    args = parser.parse_args()

    if args.command == "dump-latency":
        cmd_dump_latency()
    elif args.command == "dump-sink-bounds":
        cmd_dump_sink_bounds()
    elif args.command == "play-burst":
        cmd_play_burst(args.mac)
    elif args.command == "record-detect":
        cmd_record_detect(record_sec=args.seconds, device=args.device)
    elif args.command == "correct":
        cmd_correct(args.mac, args.ms, allow_decrease=args.allow_decrease)
    elif args.command == "sync-once":
        cmd_sync_once(record_sec=args.seconds, dry_run=args.dry_run)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
