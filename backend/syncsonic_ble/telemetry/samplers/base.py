"""Sampler base class for the Slice 1 telemetry collector.

A Sampler is the smallest reusable unit of "go look at one thing in the
live system every N seconds and emit events about it." The Collector
owns a list of Sampler instances and ticks them in order. Each Sampler
declares its own ``interval_sec`` so the Collector can run them at
different rates without spawning a thread per sampler.
"""

from __future__ import annotations

import time


class Sampler:
    """Base class for a polling sampler.

    Subclasses set ``name`` and ``interval_sec`` as class attributes (or
    in __init__) and implement ``tick()`` to do the actual work. The
    Collector handles scheduling and exception isolation.
    """

    name: str = "abstract"
    interval_sec: float = 1.0

    def __init__(self) -> None:
        self._next_due_monotonic: float = 0.0

    def setup(self) -> None:
        """Optional one-shot initialisation. Called once before the first tick."""

    def teardown(self) -> None:
        """Optional one-shot cleanup. Called once at collector shutdown."""

    def tick(self) -> None:
        """Do one unit of work. Implementations must not raise."""
        raise NotImplementedError

    def is_due(self, now_monotonic: float) -> bool:
        return now_monotonic >= self._next_due_monotonic

    def mark_ticked(self, now_monotonic: float) -> None:
        # Schedule the NEXT tick at exactly interval_sec from now, so a
        # slow sampler does not silently double-up on the following tick.
        self._next_due_monotonic = now_monotonic + self.interval_sec

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Sampler {self.name} every {self.interval_sec}s>"
