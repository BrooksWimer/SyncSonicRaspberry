# SyncSonic backend — mypy audit baseline

Baseline captured **2026-05-14**. Initial pass found **20 errors across 11 files**; the same-day follow-up cleanup pass removed 8 of those (6 stale `# type: ignore` comments + 2 trivial annotation gaps), leaving **12 errors across 5 files** as of the latest mypy run.

Run:

```bash
cd backend
python -m mypy --config-file pyproject.toml syncsonic_ble measurement
```

Config: see [`pyproject.toml`](pyproject.toml) `[tool.mypy]`. Briefly:

- `python_version = "3.11"` (matches the Pi runtime)
- `ignore_missing_imports = true` (dbus / BlueZ helpers lack stubs)
- `explicit_package_bases = true` (required for the flat `syncsonic_ble/` + `measurement/` layout)
- `check_untyped_defs = false` — gradual adoption; flipping this to `true` today raises the noise floor from 12 errors to >100. Plan: fix the remaining 10 real errors below, then flip the flag.
- `warn_unused_ignores = true` (catches stale `# type: ignore` comments)

**Current total:** 12 errors across 5 files. All 12 are real findings, not intentional silences.

## Real type-narrowing bugs (8)

These are the highest-priority items. Each is a `str | None` (or `Any | None`) being passed to a function that expects a non-null `str`. Today they "work" because the callers happen to filter null upstream — but a refactor could break it without warning.

### `syncsonic_ble/helpers/adapter_helpers.py`

- **line 61** — `Item "None" of "Any | None" has no attribute "get_object"` — `[union-attr]`
- **line 67** — same, second call site.

Likely a missing `if proxy is None: return` guard before `.get_object(...)`.

### `syncsonic_ble/state_management/connection_manager.py`

Six call sites passing a potentially-None MAC string into the BlueZ D-Bus helpers:

- **line 500** — `Argument 1 to "pair_device_dbus" has incompatible type "str | None"; expected "str"` — `[arg-type]`
- **line 510** — `remove_device_dbus`
- **line 521** — `trust_device_dbus`
- **line 536** — `connect_device_dbus`
- **line 545** — `remove_device_dbus`
- **line 603** — `remove_device_dbus`

Suggests the calling code resolves a MAC via `.get(...)` or similar pattern that returns `Optional[str]` and never narrows before the D-Bus call. Fix: narrow with an early-return guard or `assert mac is not None`.

### `syncsonic_ble/state_management/device_manager.py`

- **line 125** — `Argument 1 has incompatible type "Any | None"; expected "str"` — `[arg-type]`
- **line 136** — `Argument 1 to "_handle_new_connection" of "DeviceManager" has incompatible type "Any | None"; expected "str"` — `[arg-type]`

Same pattern: D-Bus property reads return `Any | None`, callers don't narrow.

## Real annotation gaps (2 remaining)

### `syncsonic_ble/helpers/pulseaudio_helpers.py:132`

- `Incompatible return value type (got "None", expected "str")` — `[return-value]`

A return path falls through to an implicit `None` while the function is typed to return `str`. Either narrow the type to `Optional[str]` or add a fallback return. Touches the PulseAudio sink path — has runtime impact, defer to a slice with operator review.

### `syncsonic_ble/main.py:64`

- `List item 1 has incompatible type "str | None"; expected "str | bytes | PathLike[str] | PathLike[bytes]"` — `[list-item]`

`subprocess`-style argv being built with a potentially-None element. Narrow upstream. Touches process startup; defer.

### Fixed in the same-day cleanup pass

- ~~`syncsonic_ble/state_change/action_planning.py:33`~~ — typed as `dict[str, list[str]]` based on `.setdefault(dev_mac, []).append(ctrl_mac)` usage at line 70.
- ~~`syncsonic_ble/infra/gatt_service.py:76`~~ — typed as `list[dbus.service.Object]` matching `add_characteristic(ch: dbus.service.Object)`.

## Unused `# type: ignore` comments (all 6 removed in same-day cleanup)

These were stale — mypy used to flag the call as broken, but with `ignore_missing_imports = true` they're handled at the config layer and the inline ignore is redundant. Removed in the cleanup pass:

- ~~`syncsonic_ble/helpers/sonos_discovery.py:24`~~ — `import soco.discovery`
- ~~`syncsonic_ble/helpers/sonos_controller.py:37`~~ — `import soco`
- ~~`measurement/calibrate_one.py:401`~~ — `from syncsonic_ble.helpers import sonos_controller`
- ~~`measurement/calibrate_one.py:661`~~ — `from measurement.analyze_lag import ...`
- ~~`measurement/calibrate_anchor.py:178`~~ — `from syncsonic_ble.helpers import sonos_controller`
- ~~`measurement/calibrate_anchor.py:197`~~ — `from syncsonic_ble.helpers import sonos_controller`

## Suppressed by per-module overrides

The `pyproject.toml` has narrow `disable_error_code = ["attr-defined"]` overrides for two cases that aren't real bugs:

1. **`syncsonic_ble.infra.gatt_service`** — DBusPathMixin reads `self.path` defined in mixed-in subclasses. Modeling this properly needs a `Protocol[path: str]` declaration; not blocking.
2. **`measurement._filter_ctl`, `measurement.calibrate_one`, `syncsonic_ble.helpers.pipewire_transport`, `syncsonic_ble.coordinator.coordinator`** — `socket.AF_UNIX` is Linux/Mac-only. The Pi is Linux, so this is correct at runtime. mypy on a Windows host reads the Windows typeshed and reports `AF_UNIX` as missing; on Linux CI runners it would type-check cleanly. Override silences the false positive without losing other coordinator / pipewire type checks.

## How to fix (remaining 10)

1. **Connection / device manager `str | None` (8 errors)** — bulk-fix by adding `assert mac is not None, "mac required"` near the top of each helper call site, OR narrowing the caller path so MACs are guaranteed before the D-Bus boundary. **Pi validation recommended** because these touch BlueZ pairing flow.
2. **`pulseaudio_helpers.py:132` return type** — decide between widening the return to `Optional[str]` (lets callers see the failure mode) or keeping `str` and adding a fallback. Trade-off is whether silent fallback or explicit-None-handling is the desired contract. Touches PulseAudio sink lookup; runtime-sensitive.
3. **`main.py:64` list-item** — narrow the argv source so it can't carry `None`. Touches process startup; behavior-sensitive.

Once the 8 `[arg-type]` / `[union-attr]` errors are fixed, flipping `check_untyped_defs = true` is the right next step. That will surface the much larger backlog of implicit-`Any` function bodies, but at least the type-narrowing bugs are gone first.

## Not in CI yet

Mypy is **not yet wired into `.github/workflows/ci.yml`**. Two ways to add it:

- **Hard fail on any error** — would block PRs today because of the 20 baseline errors. Bad without first fixing them.
- **Run mypy but allow failure** (`continue-on-error: true`) — runs the audit on every PR, captures regressions as a non-blocking signal. Reasonable interim.
- **Hard fail with `# type: ignore[<code>]` baseline** — mypy will only fail on *new* errors. Cleanest, but requires writing those ignores or generating a baseline file. mypy's `--baseline` flag (or a similar wrapper) makes this manageable.

Recommend the second option as the immediate next slice — gives operator-visible regression detection without breaking PR throughput.
