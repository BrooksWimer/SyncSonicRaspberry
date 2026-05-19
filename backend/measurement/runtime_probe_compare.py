"""Slice-0 ultrasonic-vs-in-band probe comparison harness.

The script generates two deterministic reference WAVs:

* in-band active stimulus: pause music, inject a short audible chirp
* ultrasonic stimulus: full-amplitude 18.5 kHz tone burst by default

It can also summarize captured WAVs from the operator's Pi run, but it
does not control PipeWire, pause music, or deploy anything remotely.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from measurement.analyze_lag import estimate_lag_samples, load_wav_mono  # noqa: E402
from measurement.probe_signals import (  # noqa: E402
    DEFAULT_ULTRASONIC_HZ,
    SAMPLE_RATE_HZ,
    write_probe_wavs,
)


def hf_power_db(
    signal: np.ndarray,
    sample_rate: int,
    *,
    center_hz: float = DEFAULT_ULTRASONIC_HZ,
    bandwidth_hz: float = 1_000.0,
) -> float:
    """Return high-frequency band power as peak-equivalent dBFS.

    A full-scale pure sine at ``center_hz`` reports approximately 0 dBFS.
    Silence reports ``-inf``.
    """
    mono = np.asarray(signal, dtype=np.float64)
    if mono.ndim == 2:
        mono = mono.mean(axis=1)
    if mono.size == 0:
        raise ValueError("signal must be non-empty")

    max_abs = float(np.max(np.abs(mono)))
    if max_abs > 1.5:
        mono = mono / 32768.0

    window = np.hanning(mono.size)
    spectrum = np.fft.rfft(mono * window)
    freqs = np.fft.rfftfreq(mono.size, d=1.0 / float(sample_rate))
    half_bw = bandwidth_hz / 2.0
    band = (freqs >= center_hz - half_bw) & (freqs <= center_hz + half_bw)
    if not np.any(band):
        return float("-inf")

    band_time = np.fft.irfft(spectrum * band, n=mono.size)
    rms = float(np.sqrt(np.mean(band_time * band_time)))
    if rms <= 1e-15:
        return float("-inf")
    peak_equivalent = rms * math.sqrt(2.0)
    return 20.0 * math.log10(max(peak_equivalent, 1e-15))


def _analyze_pair(reference: Path, captured: Path, ultrasonic_hz: float) -> dict[str, Any]:
    ref, ref_sr = load_wav_mono(reference)
    cap, cap_sr = load_wav_mono(captured)
    if ref_sr != cap_sr:
        raise ValueError(f"sample-rate mismatch: {reference}={ref_sr}, {captured}={cap_sr}")

    estimate = estimate_lag_samples(ref, cap, sample_rate=ref_sr)
    return {
        "reference": str(reference),
        "captured": str(captured),
        "sample_rate_hz": ref_sr,
        "lag_samples": estimate.lag_samples,
        "lag_ms": estimate.lag_ms,
        "peak_correlation": estimate.peak_correlation,
        "confidence_primary": estimate.confidence_primary,
        "confidence_secondary": estimate.confidence_secondary,
        "captured_hf_power_dbfs": hf_power_db(cap, cap_sr, center_hz=ultrasonic_hz),
    }


def _print_operator_notes(paths: dict[str, Path], skip_ultrasonic: bool) -> None:
    print("Generated probe WAVs:")
    for name, path in paths.items():
        print(f"  {name}: {path}")
    print()
    print("Operator Pi workflow:")
    print("  1. Copy these WAVs to syncsonic@10.0.0.89.")
    print("  2. For in-band: briefly pause music, record virtual_out.monitor + mic, paplay the in-band WAV, then resume music.")
    if not skip_ultrasonic:
        print("  3. For ultrasonic: record virtual_out.monitor + mic while playing the ultrasonic WAV at normal output level.")
    print("  4. Re-run this CLI with --inband-capture and/or --ultrasonic-capture to summarize returned WAVs.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("/tmp/syncsonic-runtime-probes"),
        help="directory for generated probe WAVs",
    )
    parser.add_argument(
        "--ultrasonic-hz",
        type=float,
        default=DEFAULT_ULTRASONIC_HZ,
        help="ultrasonic probe frequency; operator-selected default is 18.5 kHz",
    )
    parser.add_argument(
        "--skip-ultrasonic",
        action="store_true",
        help="generate only the in-band probe for environments where ultrasonic playback should be avoided",
    )
    parser.add_argument("--inband-capture", type=Path, help="optional mic capture from the in-band run")
    parser.add_argument("--ultrasonic-capture", type=Path, help="optional mic capture from the ultrasonic run")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON summary")
    args = parser.parse_args(argv)

    paths = write_probe_wavs(
        args.out_dir,
        ultrasonic_hz=args.ultrasonic_hz,
        skip_ultrasonic=args.skip_ultrasonic,
    )

    summary: dict[str, Any] = {
        "probe_dir": str(args.out_dir),
        "ultrasonic_hz": args.ultrasonic_hz,
        "skip_ultrasonic": args.skip_ultrasonic,
        "generated": {name: str(path) for name, path in paths.items()},
        "analysis": {},
    }
    if args.inband_capture:
        summary["analysis"]["inband"] = _analyze_pair(paths["inband"], args.inband_capture, args.ultrasonic_hz)
    if args.ultrasonic_capture:
        if "ultrasonic" not in paths:
            raise SystemExit("--ultrasonic-capture requires ultrasonic generation; remove --skip-ultrasonic")
        summary["analysis"]["ultrasonic"] = _analyze_pair(
            paths["ultrasonic"],
            args.ultrasonic_capture,
            args.ultrasonic_hz,
        )

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_operator_notes(paths, args.skip_ultrasonic)
        if summary["analysis"]:
            print()
            print(json.dumps(summary["analysis"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
