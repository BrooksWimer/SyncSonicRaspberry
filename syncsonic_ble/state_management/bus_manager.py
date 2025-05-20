# bus_manager.py
"""bus_manager
===============
Thread‑safe **singleton** accessor for the system D‑Bus.

*   Uses **pydbus.SystemBus()** (same API you already call elsewhere).
*   Creates the connection once; afterwards every thread gets the exact same
    object.
*   No asyncio, no extra dependencies – drop‑in for the existing synchronous
    codebase.

Usage
-----
```python
from bus_manager import get_bus
bus = get_bus()              # safe in any thread
```
The connection lives until the Python interpreter exits; BlueZ cleans up the
socket automatically, so you don't need an explicit shutdown.
"""

from __future__ import annotations

import threading
from typing import Optional
import dbus

# ---------------------------------------------------------------------------
# Internal synchronisation primitives
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()          # guards first-time creation
_BUS: Optional[dbus.bus.BusConnection] = None  # the singleton instance

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_bus() -> dbus.bus.BusConnection:
    """Return the process-wide :class:`dbus.SystemBus`.

    A single dbus.SystemBus() connection is lazily created and reused by every
    thread. 
    
    """

    global _BUS

    if _BUS is None:
        # Fast path failed – only then pay the cost of locking.
        with _LOCK:
            # Double-checked locking pattern: another thread may have created
            # the bus while we were waiting for the lock.
            if _BUS is None:
                _BUS = dbus.SystemBus()
    return _BUS
