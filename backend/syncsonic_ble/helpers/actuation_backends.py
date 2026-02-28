from __future__ import annotations

import os
from dataclasses import dataclass

from syncsonic_ble.helpers.pipewire_control_plane import (
    clear_output_control,
    publish_output_control,
)
from syncsonic_ble.helpers.pipewire_runtime import has_pipewire_cli
from syncsonic_ble.helpers.pulseaudio_helpers import create_loopback, remove_loopback_for_device
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ActuationApplyResult:
    ok: bool
    backend: str
    reason: str = ""
    applied_delay_ms: float = 0.0
    applied_rate_ppm: float = 0.0
    control_path: str = ""
    shadow_fallback: bool = False


class BaseActuationBackend:
    name = "base"

    def apply_control(self, mac: str, delay_ms: float, rate_ppm: float, *, mode: str) -> ActuationApplyResult:
        raise NotImplementedError

    def remove_output(self, mac: str) -> None:
        raise NotImplementedError


class PulseAudioLoopbackBackend(BaseActuationBackend):
    name = "pulseaudio-loopback"

    def apply_control(self, mac: str, delay_ms: float, rate_ppm: float, *, mode: str) -> ActuationApplyResult:
        sink_prefix = f"bluez_sink.{mac.replace(':', '_')}"
        ok = create_loopback(sink_prefix, latency_ms=int(round(delay_ms)))
        return ActuationApplyResult(
            ok=ok,
            backend=self.name,
            reason="" if ok else "loopback_apply_failed",
            applied_delay_ms=float(delay_ms),
            applied_rate_ppm=0.0,
        )

    def remove_output(self, mac: str) -> None:
        remove_loopback_for_device(mac)


class PipeWireNodeBackend(BaseActuationBackend):
    """Publishes delay/rate targets to the SyncSonic PipeWire control plane."""

    name = "pipewire-node"

    def apply_control(self, mac: str, delay_ms: float, rate_ppm: float, *, mode: str) -> ActuationApplyResult:
        runtime_available = has_pipewire_cli()
        control_path = publish_output_control(
            mac,
            delay_ms=delay_ms,
            rate_ppm=rate_ppm,
            mode=mode,
            active=True,
        )
        return ActuationApplyResult(
            ok=runtime_available,
            backend=self.name,
            reason=(
                "control_plane_published"
                if runtime_available
                else "pipewire_runtime_unavailable"
            ),
            applied_delay_ms=float(delay_ms),
            applied_rate_ppm=float(rate_ppm),
            control_path=control_path,
        )

    def remove_output(self, mac: str) -> None:
        clear_output_control(mac)


class PipeWireShadowBackend(BaseActuationBackend):
    """Publishes PipeWire control intent while retaining PulseAudio fallback actuation."""

    name = "pipewire-shadow"

    def __init__(self) -> None:
        self._fallback = PulseAudioLoopbackBackend()

    def apply_control(self, mac: str, delay_ms: float, rate_ppm: float, *, mode: str) -> ActuationApplyResult:
        runtime_available = has_pipewire_cli()
        control_path = publish_output_control(
            mac,
            delay_ms=delay_ms,
            rate_ppm=rate_ppm,
            mode=mode,
            active=True,
        )
        fallback = self._fallback.apply_control(mac, delay_ms, rate_ppm, mode=mode)
        return ActuationApplyResult(
            ok=fallback.ok,
            backend=self.name,
            reason=(
                "shadow_fallback"
                if runtime_available and fallback.ok
                else "pipewire_runtime_unavailable_shadow_fallback"
                if fallback.ok
                else fallback.reason
            ),
            applied_delay_ms=float(delay_ms),
            applied_rate_ppm=float(rate_ppm),
            control_path=control_path,
            shadow_fallback=True,
        )

    def remove_output(self, mac: str) -> None:
        clear_output_control(mac)
        self._fallback.remove_output(mac)


def get_actuation_backend() -> BaseActuationBackend:
    backend_name = os.getenv("SYNCSONIC_ACTUATION_BACKEND", "pulseaudio-loopback").strip().lower()
    if backend_name == "pipewire-node":
        return PipeWireNodeBackend()
    if backend_name == "pipewire-shadow":
        return PipeWireShadowBackend()
    return PulseAudioLoopbackBackend()
