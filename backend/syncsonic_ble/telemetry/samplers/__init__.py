"""Sampler implementations for the Slice 1 telemetry collector.

A Sampler is a small object the Collector schedules at a fixed interval.
It pulls one snapshot of state from the live system on each tick and
emits zero or more events into the EventWriter. Samplers must not raise
out of tick(); the Collector wraps every call but a clean implementation
catches its own subprocess and dbus errors and emits nothing rather
than logging noise.

The four Slice 1 samplers:

- RssiSampler          (1 Hz)   per-speaker hcitool rssi
- PwNodeSampler        (1 Hz)   pw-dump snapshot of all PipeWire nodes
- BluezTransportSampler (0.2 Hz) MediaTransport1 props per device
- XrunTailSampler      (event)  tails journalctl for graph xrun events

XrunTailSampler is structurally different from the polling three (it
runs a long-lived subprocess and emits when lines arrive); it is its
own commit so this one stays focused on the polling shape.
"""

from syncsonic_ble.telemetry.samplers.base import Sampler

__all__ = ["Sampler"]
