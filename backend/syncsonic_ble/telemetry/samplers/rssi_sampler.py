"""Per-speaker RSSI sampler.

Samples per-second, maintains a 10-sample rolling window per MAC for the
short-term median (the "current" RSSI to display) and a 60-sample
rolling window for the longer-term baseline (the "this is what this
speaker normally sits at" reference). Emits one ``rssi_sample`` per
speaker per second and one ``rssi_baseline`` per speaker per 10 seconds.

The 2026-04-29 field experiment in proposal Section 9 established RSSI
as the leading indicator of audible dropouts and showed that single
samples are too noisy to be useful (the same physical configuration
read -14 dBm and -25 dBm on different single-shot reads of the same
speaker). Rolling medians are the minimum reliable read.

Discovery
---------
The set of "currently relevant" (hci, mac) pairs is the set of
A2DP-sink devices that are Connected on a non-reserved adapter. We
discover this once at setup and re-discover every DISCOVERY_REFRESH_SEC
to pick up new connects/disconnects. A future commit may switch this
to listen on the BLUEZ_CONNECT/DISCONNECT events instead.

Subprocess vs D-Bus
-------------------
RSSI is queried via ``hcitool -i <hci> rssi <mac>`` because that is the
only interface that returns a real-time L2CAP-level RSSI for a Connected
device. The BlueZ Device1.RSSI property is only populated for scanned
(not yet connected) devices. Discovery uses python-dbus directly via
the existing ``adapter_helpers`` so we do not spawn one subprocess per
adapter just to enumerate.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Deque, Dict, List, Optional, Tuple

import dbus

from syncsonic_ble.helpers.adapter_helpers import set_bus
from syncsonic_ble.telemetry import EventType
from syncsonic_ble.telemetry.event_writer import emit
from syncsonic_ble.telemetry.samplers.base import Sampler
from syncsonic_ble.utils.constants import (
    ADAPTER_INTERFACE,
    BLUEZ_SERVICE_NAME,
    DBUS_OM_IFACE,
    DEVICE_INTERFACE,
)
from syncsonic_ble.utils.logging_conf import get_logger

log = get_logger(__name__)

DISCOVERY_REFRESH_SEC = 5.0
ROLLING_10S_LEN = 10
ROLLING_60S_LEN = 60
BASELINE_EMIT_INTERVAL_SEC = 10.0
HCITOOL_TIMEOUT_SEC = 1.0

_RSSI_RE = re.compile(r"RSSI return value:\s*(-?\d+)")


@dataclass(frozen=True)
class RssiSnapshot:
    """Immutable per-speaker RSSI summary for in-process consumers (Slice 3.3).

    The Coordinator polls this snapshot every tick to decide whether
    a speaker's link quality has dropped enough to warrant a
    preemptive soft-mute. Both medians are over the rolling deques
    the sampler already maintains, so this struct adds zero work to
    the audio path; it's a free lunch from the data the sampler is
    collecting anyway.
    """
    mac: str
    hci: str
    latest_dbm: int
    median_10s: float
    median_60s: float
    n_samples_10s: int
    n_samples_60s: int
    last_sample_monotonic_ns: int


# Module-level shared state populated by the singleton RssiSampler in
# its tick() and read by the Coordinator. Both live in the same
# process (syncsonic_ble.main) so a plain dict + lock is the right
# primitive; we don't need a queue or pub-sub channel.
_LATEST_RSSI: Dict[str, RssiSnapshot] = {}
_LATEST_RSSI_LOCK = threading.Lock()


def get_latest_rssi(mac: str) -> Optional[RssiSnapshot]:
    """Return the most recent RssiSnapshot for ``mac``, or None if no
    samples have been collected yet. Thread-safe; intended for the
    Coordinator and any future in-process consumers (e.g. the BLE
    notification surface in Slice 3.6)."""
    with _LATEST_RSSI_LOCK:
        return _LATEST_RSSI.get(mac.upper())


def get_all_latest_rssi() -> Dict[str, RssiSnapshot]:
    """Return a snapshot copy of the latest RSSI dict. Used by tests
    and the BLE notification surface."""
    with _LATEST_RSSI_LOCK:
        return dict(_LATEST_RSSI)


class RssiSampler(Sampler):
    name = "rssi"
    interval_sec = 1.0

    def __init__(self, bus: Optional[dbus.SystemBus] = None) -> None:
        super().__init__()
        # Either accept a shared bus from the collector, or open our own.
        # Sharing is preferred (one connection per process) but not
        # required for correctness; python-dbus internally singletons
        # SystemBus per-thread.
        self._bus = bus or dbus.SystemBus()
        set_bus(self._bus)
        self._reserved_hci = os.environ.get("RESERVED_HCI", "")
        self._targets: List[Tuple[str, str]] = []  # list of (hci_name, mac)
        self._target_refresh_at: float = 0.0
        self._rolling_10: Dict[str, Deque[int]] = {}
        self._rolling_60: Dict[str, Deque[int]] = {}
        self._last_baseline_emit: Dict[str, float] = {}

    def tick(self) -> None:
        now = time.monotonic()
        if now >= self._target_refresh_at:
            try:
                self._targets = self._discover_targets()
            except Exception as exc:  # noqa: BLE001
                log.debug("RssiSampler discover failed: %s", exc)
                self._targets = []
            self._target_refresh_at = now + DISCOVERY_REFRESH_SEC

        for hci_name, mac in self._targets:
            sample = self._query_rssi(hci_name, mac)
            if sample is None:
                continue
            d10 = self._rolling_10.setdefault(mac, deque(maxlen=ROLLING_10S_LEN))
            d60 = self._rolling_60.setdefault(mac, deque(maxlen=ROLLING_60S_LEN))
            d10.append(sample)
            d60.append(sample)
            m10 = float(median(d10))
            m60 = float(median(d60))
            # Update shared snapshot for in-process consumers (Slice 3.3
            # Coordinator). We do this every tick so the Coordinator
            # always sees the freshest data, not just the every-10s
            # baseline emit cadence.
            with _LATEST_RSSI_LOCK:
                _LATEST_RSSI[mac] = RssiSnapshot(
                    mac=mac,
                    hci=hci_name,
                    latest_dbm=int(sample),
                    median_10s=m10,
                    median_60s=m60,
                    n_samples_10s=len(d10),
                    n_samples_60s=len(d60),
                    last_sample_monotonic_ns=time.monotonic_ns(),
                )
            emit(EventType.RSSI_SAMPLE, {
                "mac": mac,
                "hci": hci_name,
                "rssi_dbm": sample,
                "median_10s": m10,
                "n_samples_10s": len(d10),
            })
            last_baseline = self._last_baseline_emit.get(mac, 0.0)
            if now - last_baseline >= BASELINE_EMIT_INTERVAL_SEC:
                emit(EventType.RSSI_BASELINE, {
                    "mac": mac,
                    "hci": hci_name,
                    "median_10s": m10,
                    "median_60s": m60,
                    "n_samples_60s": len(d60),
                })
                self._last_baseline_emit[mac] = now

    def _discover_targets(self) -> List[Tuple[str, str]]:
        """Enumerate (hci_name, mac) for Connected devices on non-reserved adapters."""
        om = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
        objects = om.GetManagedObjects()
        # Build adapter_path -> hci_name map first so we can label devices.
        hci_for_path: Dict[str, str] = {}
        for path, ifaces in objects.items():
            if ADAPTER_INTERFACE in ifaces:
                hci_name = path.rsplit("/", 1)[-1]
                hci_for_path[path] = hci_name
        targets: List[Tuple[str, str]] = []
        seen_macs: set[str] = set()
        for path, ifaces in objects.items():
            dev = ifaces.get(DEVICE_INTERFACE)
            if not dev or not dev.get("Connected", False):
                continue
            adapter_path = "/".join(path.split("/")[:4])
            hci_name = hci_for_path.get(adapter_path, "")
            if not hci_name or hci_name == self._reserved_hci:
                continue
            mac = str(dev.get("Address", "")).upper()
            if not mac or mac in seen_macs:
                continue
            seen_macs.add(mac)
            targets.append((hci_name, mac))
        return targets

    def _query_rssi(self, hci_name: str, mac: str) -> Optional[int]:
        try:
            result = subprocess.run(
                ["hcitool", "-i", hci_name, "rssi", mac],
                capture_output=True,
                text=True,
                timeout=HCITOOL_TIMEOUT_SEC,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            log.debug("hcitool rssi failed for %s on %s: %s", mac, hci_name, exc)
            return None
        if result.returncode != 0:
            return None
        m = _RSSI_RE.search(result.stdout or "")
        if not m:
            return None
        try:
            return int(m.group(1))
        except ValueError:
            return None
