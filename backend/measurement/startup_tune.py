"""Known reference waveform for Slice 4 startup-tune calibration.

A short band-limited linear chirp (rising \"whoop\") is easy for the
Slice 4.1 cross-correlator to lock onto, stays away from extreme LF/HF
where rooms and cheap speakers are unruly, and is usually perceived as
non-abrasive compared to pink noise at similar SPL.

The WAV is written under ``/run/syncsonic/slice4_startup_tune/`` (tmpfs
on the Pi service layout) and replayed with ``paplay`` into the
``virtual_out`` sink during calibration while ``virtual_out.monitor``
and the USB mic are recorded.

Wi-Fi output integration note
-----------------------------
High-delay Wi-Fi speakers need the same measurement path as Bluetooth:
the analyzer only cares that *one* output is unmuted and that the
reference tap matches what was fed into the graph. Larger measured lags
still land inside ``analyze_lag``'s search window as long as phone/music
bleed does not dominate the reference capture — pause phone playback
for best results.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

import numpy as np

SR = 48_000
# Short enough for a \"quick tune\"; long enough for stable correlation.
_CHIRP_DURATION_SEC = 2.65
_F0_HZ = 420.0
_F1_HZ = 3400.0
_FADE_SEC = 0.07

DEFAULT_TUNE_DIR = Path(
    os.environ.get("SYNCSONIC_STARTUP_TUNE_DIR", "/run/syncsonic/slice4_startup_tune"),
)
TUNE_FILENAME = "syncsonic_startup_chirp_v2.wav"


def linear_chirp_mono(duration_sec: float, sr: int, f0: float, f1: float) -> np.ndarray:
    """Linear FM sine chirp from ``f0`` to ``f1`` over ``duration_sec``."""
    n = int(round(duration_sec * sr))
    if n < 16:
        raise ValueError("chirp too short")
    t = np.arange(n, dtype=np.float64) / sr
    phase = 2.0 * np.pi * (f0 * t + 0.5 * (f1 - f0) * (t * t) / duration_sec)
    return np.sin(phase)


def raised_cosine_fade(signal: np.ndarray, sr: int, fade_sec: float) -> np.ndarray:
    """Apply symmetric raised-cosine fades at both ends."""
    fl = max(1, int(round(fade_sec * sr)))
    if 2 * fl >= len(signal):
        fl = max(1, len(signal) // 4)
    out = signal.astype(np.float64, copy=True)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, fl, endpoint=False))
    out[:fl] *= ramp
    out[-fl:] *= ramp[::-1]
    return out


def write_stereo_s16_wav(path: Path, mono_fp: np.ndarray, sr: int) -> None:
    """Write mono float [-1,1] as stereo int16 LE WAV."""
    mono_fp = np.clip(mono_fp, -1.0, 1.0)
    mono_i16 = (mono_fp * 32767.0).astype(np.int16)
    stereo = np.stack([mono_i16, mono_i16], axis=1).reshape(-1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(stereo.tobytes())


def build_startup_tune_mono(sample_rate: int = SR) -> np.ndarray:
    """Synthesize the canonical chirp (float mono, peak magnitude <= 1).

    Peak amplitude is the SNR knob for the Wi-Fi anchor measurement.
    A debug capture at 0.22 peak through the Sonos / Icecast / MP3
    chain produced a flat correlation surface where the real lag
    peak (~5021 ms) was within 5 % of every noise sidelobe between
    300 ms and 5 s. Bumping to 0.40 (+5 dB) gives the cross-correlator
    enough margin to lock onto the true Sonos peak while staying well
    below clipping. BT speakers had ~70x confidence at 0.22 so they
    have plenty of headroom for the louder chirp.
    """
    sig = linear_chirp_mono(_CHIRP_DURATION_SEC, sample_rate, _F0_HZ, _F1_HZ)
    sig = raised_cosine_fade(sig, sample_rate, _FADE_SEC)
    peak = float(np.max(np.abs(sig))) if sig.size else 0.0
    target_peak = 0.40
    if peak > 0:
        sig = sig * (target_peak / peak)
    return sig.astype(np.float64)


def wav_duration_sec(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def ensure_startup_tune_wav(
    dest_dir: Path | None = None,
    *,
    force_regenerate: bool = False,
) -> Path:
    """Return path to the startup chirp WAV, creating it if needed."""
    base = dest_dir or DEFAULT_TUNE_DIR
    path = base / TUNE_FILENAME
    if force_regenerate or os.environ.get("SYNCSONIC_REGENERATE_STARTUP_TUNE", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        if path.exists():
            path.unlink()
    if path.exists() and path.stat().st_size > 2048:
        return path
    mono = build_startup_tune_mono(SR)
    write_stereo_s16_wav(path, mono, SR)
    return path
