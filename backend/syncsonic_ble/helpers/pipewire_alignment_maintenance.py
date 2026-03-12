from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from syncsonic_ble.helpers.device_labels import format_device_label
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class OutputTelemetry:
    observed_latency_ms: Optional[float] = None
    sink_state: str = "UNKNOWN"
    sink_name: str = ""
    seen: bool = False


@dataclass
class OutputMaintenanceState:
    mac: str
    baseline_latency_ms: Optional[float] = None
    ema_latency_ms: Optional[float] = None
    drift_ms: float = 0.0
    fine_rate_ppm: float = 0.0
    auto_trim_ms: float = 0.0
    confidence: float = 0.35
    health_state: str = "acquiring"
    remeasure_required: bool = False
    baseline_samples: int = 0
    missing_ticks: int = 0
    reconnect_events: int = 0
    dropout_events: int = 0
    coarse_corrections: int = 0
    fine_corrections: int = 0
    latency_spike_events: int = 0
    last_sink_state: str = "UNKNOWN"
    last_observed_latency_ms: float = 0.0
    updated_at: float = 0.0


class PipeWireAlignmentMaintenance:
    """Maintains baseline latency models and bounded self-heal corrections.

    The controller purposefully uses only runtime transport telemetry:
    - sink state transitions
    - effective sink-input latency (buffer + sink)
    - missing/reconnect events

    It outputs bounded "fine" and "coarse" corrections per output:
    - fine: temporary ppm intent proportional to drift
    - coarse: small delay trim step to recenter target latency
    """

    SAMPLE_INTERVAL_SEC = float(os.environ.get("SYNCSONIC_SELF_HEAL_SAMPLE_SEC", "1.0"))
    EMA_ALPHA = float(os.environ.get("SYNCSONIC_SELF_HEAL_EMA_ALPHA", "0.2"))
    BASELINE_MIN_SAMPLES = int(os.environ.get("SYNCSONIC_SELF_HEAL_BASELINE_SAMPLES", "8"))
    BASELINE_ADAPT_ALPHA = float(os.environ.get("SYNCSONIC_SELF_HEAL_BASELINE_ADAPT_ALPHA", "0.01"))
    FINE_THRESHOLD_MS = float(os.environ.get("SYNCSONIC_SELF_HEAL_FINE_THRESHOLD_MS", "4.0"))
    COARSE_THRESHOLD_MS = float(os.environ.get("SYNCSONIC_SELF_HEAL_COARSE_THRESHOLD_MS", "20.0"))
    SPIKE_THRESHOLD_MS = float(os.environ.get("SYNCSONIC_SELF_HEAL_SPIKE_THRESHOLD_MS", "40.0"))
    RATE_PPM_PER_MS = float(os.environ.get("SYNCSONIC_SELF_HEAL_RATE_PPM_PER_MS", "5.0"))
    MAX_FINE_RATE_PPM = float(os.environ.get("SYNCSONIC_SELF_HEAL_MAX_FINE_RATE_PPM", "250.0"))
    MAX_TRIM_MS = float(os.environ.get("SYNCSONIC_SELF_HEAL_MAX_TRIM_MS", "250.0"))
    MAX_TRIM_STEP_MS = float(os.environ.get("SYNCSONIC_SELF_HEAL_MAX_TRIM_STEP_MS", "8.0"))
    ENABLED = os.environ.get("SYNCSONIC_ENABLE_SELF_HEAL", "1").strip().lower() not in {"0", "false", "off", "no"}
    ENABLE_PACTL_TELEMETRY = os.environ.get("SYNCSONIC_ENABLE_PACTL_TELEMETRY", "0").strip().lower() not in {"0", "false", "off", "no"}

    def __init__(self) -> None:
        self._states: Dict[str, OutputMaintenanceState] = {}
        self._last_sample_monotonic: float = 0.0
        self._last_telemetry: Dict[str, OutputTelemetry] = {}

    def apply(
        self,
        outputs: Dict[str, Dict[str, Any]],
        *,
        transport_base_ms: float,
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        current_macs = {mac.upper() for mac in outputs.keys()}
        for mac in list(self._states.keys()):
            if mac not in current_macs:
                self._states.pop(mac, None)

        telemetry = self._sample_telemetry()
        adjusted: Dict[str, Dict[str, Any]] = {}
        health: Dict[str, Dict[str, Any]] = {}
        for mac, cfg in outputs.items():
            normalized_mac = mac.upper()
            normalized_cfg = dict(cfg) if isinstance(cfg, dict) else {}
            state = self._states.get(normalized_mac)
            if state is None:
                state = OutputMaintenanceState(mac=normalized_mac)
                self._states[normalized_mac] = state

            target, snapshot = self._apply_output(
                state,
                normalized_cfg,
                telemetry.get(normalized_mac),
                transport_base_ms=transport_base_ms,
            )
            adjusted[normalized_mac] = target
            health[normalized_mac] = snapshot
        return adjusted, health

    def _apply_output(
        self,
        state: OutputMaintenanceState,
        cfg: Dict[str, Any],
        telemetry: Optional[OutputTelemetry],
        *,
        transport_base_ms: float,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        now = time.monotonic()
        conf_before = float(state.confidence)
        conf_reasons: List[str] = []
        active = bool(cfg.get("active", True))
        base_delay_ms = float(cfg.get("delay_ms", transport_base_ms))
        base_rate_ppm = float(cfg.get("rate_ppm", 0.0))
        mode = str(cfg.get("mode", "idle"))

        if not active:
            state.health_state = "inactive"
            state.fine_rate_ppm = 0.0
            state.updated_at = now
            return dict(cfg), asdict(state)

        if not self.ENABLE_PACTL_TELEMETRY:
            state.health_state = "telemetry_disabled"
            state.fine_rate_ppm = 0.0
            state.remeasure_required = False
            state.updated_at = now
            adjusted_delay = max(float(transport_base_ms), base_delay_ms + state.auto_trim_ms)
            updated = dict(cfg)
            updated.update(
                {
                    "delay_ms": round(adjusted_delay, 3),
                    "rate_ppm": round(base_rate_ppm, 3),
                    "mode": f"{mode}:standby",
                }
            )
            return updated, asdict(state)

        if telemetry is None or not telemetry.seen:
            state.missing_ticks += 1
            if state.missing_ticks == 1:
                state.dropout_events += 1
            state.health_state = "missing" if state.missing_ticks >= 2 else "suspect"
            state.confidence = _clamp(state.confidence - 0.12, 0.0, 1.0)
            conf_reasons.append("telemetry_missing_penalty")
            state.fine_rate_ppm = 0.0
            state.remeasure_required = state.confidence <= 0.2 or state.missing_ticks >= 4
            state.updated_at = now
            adjusted_delay = max(float(transport_base_ms), base_delay_ms + state.auto_trim_ms)
            updated = dict(cfg)
            updated.update(
                {
                    "delay_ms": round(adjusted_delay, 3),
                    "rate_ppm": round(base_rate_ppm, 3),
                    "mode": f"{mode}:recovering",
                }
            )
            self._log_confidence_change(
                state=state,
                before=conf_before,
                reasons=conf_reasons,
                mode=mode,
                telemetry=telemetry,
            )
            return updated, asdict(state)

        if telemetry.observed_latency_ms is None:
            sink_state = telemetry.sink_state.upper()
            state.health_state = "no_stream_telemetry"
            state.fine_rate_ppm = 0.0
            state.remeasure_required = False
            # Do not collapse confidence when there's no active sink-input latency yet.
            state.confidence = _clamp(state.confidence + 0.01, 0.0, 1.0)
            conf_reasons.append("no_stream_telemetry_gain")
            state.last_sink_state = sink_state
            state.updated_at = now
            adjusted_delay = max(float(transport_base_ms), base_delay_ms + state.auto_trim_ms)
            updated = dict(cfg)
            updated.update(
                {
                    "delay_ms": round(adjusted_delay, 3),
                    "rate_ppm": round(base_rate_ppm, 3),
                    "mode": f"{mode}:standby",
                }
            )
            self._log_confidence_change(
                state=state,
                before=conf_before,
                reasons=conf_reasons,
                mode=mode,
                telemetry=telemetry,
            )
            return updated, asdict(state)

        observed = float(telemetry.observed_latency_ms)
        sink_state = telemetry.sink_state.upper()
        if sink_state != state.last_sink_state:
            if state.last_sink_state == "UNKNOWN":
                pass
            elif sink_state in {"RUNNING", "IDLE"} and state.last_sink_state in {"SUSPENDED", "UNKNOWN"}:
                state.reconnect_events += 1
            elif sink_state == "SUSPENDED":
                state.dropout_events += 1
                state.confidence = _clamp(state.confidence - 0.2, 0.0, 1.0)
                conf_reasons.append("sink_suspended_penalty")
            state.last_sink_state = sink_state

        if state.missing_ticks > 0:
            state.reconnect_events += 1
            state.health_state = "recovering"
            state.baseline_samples = 0
            state.baseline_latency_ms = None
            state.missing_ticks = 0

        state.ema_latency_ms = (
            observed
            if state.ema_latency_ms is None
            else ((1.0 - self.EMA_ALPHA) * state.ema_latency_ms) + (self.EMA_ALPHA * observed)
        )
        state.last_observed_latency_ms = observed

        if state.baseline_latency_ms is None:
            state.baseline_samples += 1
            if state.baseline_samples >= self.BASELINE_MIN_SAMPLES and sink_state in {"RUNNING", "IDLE"}:
                state.baseline_latency_ms = float(state.ema_latency_ms)
                state.health_state = "tracking"
                state.confidence = _clamp(state.confidence + 0.2, 0.0, 1.0)
                conf_reasons.append("baseline_established_bonus")
            else:
                state.health_state = "acquiring"
            state.fine_rate_ppm = 0.0
            state.remeasure_required = False
            state.updated_at = now
            self._log_confidence_change(
                state=state,
                before=conf_before,
                reasons=conf_reasons,
                mode=mode,
                telemetry=telemetry,
            )
            return dict(cfg), asdict(state)

        drift_ms = float(state.ema_latency_ms) - float(state.baseline_latency_ms)
        if abs(drift_ms) >= self.SPIKE_THRESHOLD_MS:
            state.latency_spike_events += 1
            state.health_state = "recovering"
            state.confidence = _clamp(state.confidence - 0.1, 0.0, 1.0)
            conf_reasons.append("latency_spike_penalty")
        state.drift_ms = round(drift_ms, 4)

        fine_rate_ppm = 0.0
        if self.ENABLED and abs(drift_ms) >= self.FINE_THRESHOLD_MS:
            fine_rate_ppm = _clamp(-drift_ms * self.RATE_PPM_PER_MS, -self.MAX_FINE_RATE_PPM, self.MAX_FINE_RATE_PPM)
            state.fine_corrections += 1
            state.health_state = "recovering" if abs(drift_ms) >= self.COARSE_THRESHOLD_MS else "tracking"
            state.confidence = _clamp(state.confidence - 0.03, 0.0, 1.0)
            conf_reasons.append("fine_correction_penalty")
        else:
            state.confidence = _clamp(state.confidence + 0.02, 0.0, 1.0)
            conf_reasons.append("stable_tracking_gain")
            if state.health_state not in {"recovering", "missing"}:
                state.health_state = "tracking"
        state.fine_rate_ppm = round(fine_rate_ppm, 3)

        if self.ENABLED and abs(drift_ms) >= self.COARSE_THRESHOLD_MS:
            trim_step = _clamp(-drift_ms * 0.5, -self.MAX_TRIM_STEP_MS, self.MAX_TRIM_STEP_MS)
            state.auto_trim_ms = _clamp(state.auto_trim_ms + trim_step, -self.MAX_TRIM_MS, self.MAX_TRIM_MS)
            state.coarse_corrections += 1
            state.health_state = "recovering"
        state.remeasure_required = bool(state.confidence <= 0.2 or abs(drift_ms) >= (self.COARSE_THRESHOLD_MS * 2.0))

        # Slowly adapt baseline only when locked and drift is small to avoid
        # chasing transient jitter while still handling long-term runtime drift.
        if (
            state.baseline_latency_ms is not None
            and state.health_state == "tracking"
            and abs(drift_ms) <= (self.FINE_THRESHOLD_MS * 0.5)
            and state.confidence >= 0.7
        ):
            state.baseline_latency_ms = (
                (1.0 - self.BASELINE_ADAPT_ALPHA) * state.baseline_latency_ms
                + (self.BASELINE_ADAPT_ALPHA * float(state.ema_latency_ms))
            )

        adjusted_delay = max(float(transport_base_ms), base_delay_ms + state.auto_trim_ms)
        adjusted_rate = base_rate_ppm + state.fine_rate_ppm
        adjusted_mode = mode if state.health_state == "tracking" else f"{mode}:auto"
        updated = dict(cfg)
        updated.update(
            {
                "delay_ms": round(adjusted_delay, 3),
                "rate_ppm": round(adjusted_rate, 3),
                "mode": adjusted_mode,
            }
        )
        state.updated_at = now
        self._log_confidence_change(
            state=state,
            before=conf_before,
            reasons=conf_reasons,
            mode=mode,
            telemetry=telemetry,
        )
        return updated, asdict(state)

    def _log_confidence_change(
        self,
        *,
        state: OutputMaintenanceState,
        before: float,
        reasons: List[str],
        mode: str,
        telemetry: Optional[OutputTelemetry],
    ) -> None:
        after = float(state.confidence)
        delta = after - float(before)
        if abs(delta) < 1e-9:
            return
        observed = telemetry.observed_latency_ms if telemetry else None
        sink_state = telemetry.sink_state.upper() if telemetry else "UNKNOWN"
        seen = bool(telemetry and telemetry.seen)
        log.info(
            "[CONF] speaker=%s conf=%.2f->%.2f delta=%+.2f reasons=%s mode=%s health=%s "
            "sink=%s seen=%s observed=%.3f ema=%.3f baseline=%.3f drift=%.3f "
            "missing_ticks=%d baseline_samples=%d remeasure=%s",
            format_device_label(state.mac),
            before,
            after,
            delta,
            ",".join(reasons) if reasons else "unspecified",
            mode,
            state.health_state,
            sink_state,
            seen,
            float(observed) if observed is not None else -1.0,
            float(state.ema_latency_ms) if state.ema_latency_ms is not None else -1.0,
            float(state.baseline_latency_ms) if state.baseline_latency_ms is not None else -1.0,
            float(state.drift_ms),
            int(state.missing_ticks),
            int(state.baseline_samples),
            bool(state.remeasure_required),
        )

    def _sample_telemetry(self) -> Dict[str, OutputTelemetry]:
        if not self.ENABLE_PACTL_TELEMETRY:
            return {}
        now = time.monotonic()
        if now - self._last_sample_monotonic < self.SAMPLE_INTERVAL_SEC and self._last_telemetry:
            return self._last_telemetry

        sink_name_by_index, sink_state_by_mac = self._read_sinks()
        latency_by_mac = self._read_sink_input_latency(sink_name_by_index)

        telemetry: Dict[str, OutputTelemetry] = {}
        all_macs = set(latency_by_mac.keys()) | set(sink_state_by_mac.keys())
        for mac in all_macs:
            latency, sink_name = latency_by_mac.get(mac, (None, ""))
            sink_state = sink_state_by_mac.get(mac, ("UNKNOWN", sink_name))[0]
            if not sink_name:
                sink_name = sink_state_by_mac.get(mac, ("UNKNOWN", ""))[1]
            telemetry[mac] = OutputTelemetry(
                observed_latency_ms=latency,
                sink_state=sink_state,
                sink_name=sink_name,
                seen=latency is not None or sink_state != "UNKNOWN",
            )

        self._last_sample_monotonic = now
        self._last_telemetry = telemetry
        return telemetry

    def _read_sinks(self) -> Tuple[Dict[int, str], Dict[str, Tuple[str, str]]]:
        sink_name_by_index: Dict[int, str] = {}
        sink_state_by_mac: Dict[str, Tuple[str, str]] = {}

        try:
            result_short = subprocess.run(
                ["pactl", "list", "sinks", "short"],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return sink_name_by_index, sink_state_by_mac
        if result_short.returncode == 0:
            for line in result_short.stdout.splitlines():
                parts = line.split()
                if len(parts) < 2:
                    continue
                try:
                    sink_index = int(parts[0])
                except Exception:
                    continue
                sink_name_by_index[sink_index] = parts[1]

        try:
            result = subprocess.run(
                ["pactl", "list", "sinks"],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return sink_name_by_index, sink_state_by_mac
        if result.returncode != 0:
            return sink_name_by_index, sink_state_by_mac

        current_name = ""
        current_state = "UNKNOWN"
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if line.startswith("Name:"):
                current_name = line.split(":", 1)[1].strip()
                current_state = "UNKNOWN"
                continue
            if line.startswith("State:"):
                current_state = line.split(":", 1)[1].strip().upper()
                mac = self._sink_name_to_mac(current_name)
                if mac:
                    sink_state_by_mac[mac] = (current_state, current_name)
        return sink_name_by_index, sink_state_by_mac

    def _read_sink_input_latency(self, sink_name_by_index: Dict[int, str]) -> Dict[str, Tuple[Optional[float], str]]:
        try:
            result = subprocess.run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return {}
        if result.returncode != 0:
            return {}

        output: Dict[str, Tuple[Optional[float], str]] = {}
        block: List[str] = []
        for raw in result.stdout.splitlines():
            if raw.startswith("Sink Input #"):
                if block:
                    self._parse_sink_input_block(block, sink_name_by_index, output)
                block = [raw]
                continue
            if raw.strip() == "" and block:
                self._parse_sink_input_block(block, sink_name_by_index, output)
                block = []
                continue
            if block:
                block.append(raw)
        if block:
            self._parse_sink_input_block(block, sink_name_by_index, output)
        return output

    def _parse_sink_input_block(
        self,
        lines: List[str],
        sink_name_by_index: Dict[int, str],
        output: Dict[str, Tuple[Optional[float], str]],
    ) -> None:
        sink_ref = ""
        buffer_usec: Optional[float] = None
        sink_usec: Optional[float] = None
        for raw in lines:
            line = raw.strip()
            if line.startswith("Sink:"):
                sink_ref = line.split(":", 1)[1].strip()
                continue
            if "Buffer Latency:" in line:
                match = re.search(r"Buffer Latency:\s*([\d.]+)\s*usec", line)
                if match:
                    buffer_usec = float(match.group(1))
                continue
            if "Sink Latency:" in line:
                match = re.search(r"Sink Latency:\s*([\d.]+)\s*usec", line)
                if match:
                    sink_usec = float(match.group(1))

        sink_name = sink_ref
        if sink_ref.isdigit():
            sink_name = sink_name_by_index.get(int(sink_ref), sink_ref)
        mac = self._sink_name_to_mac(sink_name)
        if not mac:
            return

        if buffer_usec is None and sink_usec is None:
            output[mac] = (None, sink_name)
            return
        latency_ms = ((buffer_usec or 0.0) + (sink_usec or 0.0)) / 1000.0
        output[mac] = (latency_ms, sink_name)

    def _sink_name_to_mac(self, sink_name: str) -> str:
        if sink_name.startswith("bluez_sink."):
            token = sink_name.split(".", 1)[1].split(".", 1)[0]
            return token.replace("_", ":").upper()
        if sink_name.startswith("bluez_output."):
            token = sink_name.split(".", 1)[1].split(".", 1)[0]
            return token.replace("_", ":").upper()
        return ""
