"""SyncSonic Slice 1 measurement scripts (top-level package).

Sibling of ``syncsonic_ble``. Holds standalone scripts that orchestrate
sessions, capture audio, and generate reports. Separate from
``syncsonic_ble`` because these run as their own processes (not inside
the BLE service) and are invoked manually or by ``make session``.
"""
