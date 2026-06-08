# SyncSonic backend — mypy audit baseline

Captured **2026-05-14**. Three passes brought the count from **20 errors → 12 → 0**:

1. **Initial pass** found 20 errors across 11 files.
2. **Same-day cleanup** removed 8 (6 stale `# type: ignore` comments + 2 trivial annotation gaps).
3. **Pi-validated narrowing pass** (later same day) closed the remaining 12 — all real `str | None` / `Any | None` narrowing gaps in the BlueZ pairing flow, plus the `_BUS` lazy-init guard and the `pulseaudio_helpers.find_actual_sink_name` return type. Validated on the live Pi via `python -m compileall` + import smoke test against the running service tree.

Run:

```bash
cd backend
python -m mypy --config-file pyproject.toml syncsonic_ble measurement
```

Config: see [`pyproject.toml`](pyproject.toml) `[tool.mypy]`. Briefly:

- `python_version = "3.11"` (matches the Pi runtime)
- `ignore_missing_imports = true` (dbus / BlueZ helpers lack stubs)
- `explicit_package_bases = true` (required for the flat `syncsonic_ble/` + `measurement/` layout)
- `check_untyped_defs = false` — gradual; the suite is at zero errors with the flag off. Flipping `true` is the next step and is now an isolated decision rather than a "fix N existing bugs first" project.
- `warn_unused_ignores = true` (catches stale `# type: ignore` comments)

**Current total: 0 errors across 55 source files.** Notes about `[annotation-unchecked]` from untyped function bodies are informational — they vanish if you flip `check_untyped_defs = true` and accept the larger backlog those will surface.

## Real type-narrowing bugs (all 8 resolved in the third pass)

Each was a `str | None` (or `Any | None`) being passed to a function that expected a non-null `str`. Most "worked" in practice because upstream control flow narrowed at runtime — but a refactor or unexpected D-Bus signal variant would crash the BlueZ pairing path opaquely. All fixed and Pi-validated.

### ~~`syncsonic_ble/helpers/adapter_helpers.py`~~ — RESOLVED

- ~~**line 61** — `Item "None" of "Any | None" has no attribute "get_object"`~~
- ~~**line 67** — same, second call site.~~

**Fix:** Added a `_require_bus()` helper that raises a clear `RuntimeError` if `set_bus()` hasn't been called yet, instead of letting `None.get_object(...)` crash deep inside the dbus library with an opaque `AttributeError`. Same pattern propagated to `reset_adapter` at line ~97.

### ~~`syncsonic_ble/state_management/connection_manager.py`~~ — RESOLVED

Six call sites passing a potentially-None `device_path` into the BlueZ D-Bus helpers; one more emerged at line 569 (`remove_device_dbus`) when `device_path_on_adapter()` was re-invoked mid-flow.

**Fix:** Declared `device_path: str | None` explicitly at initialization. Added a guard before the state machine's pair/trust/connect branches: if `device_path` is None at that point, log a warning and redirect to `run_discovery` rather than diving into BlueZ with a None argument. Inside the connect-success path, the `refreshed = device_path_on_adapter(...)` is only assigned if non-None — falling back to the most-recent-good path when BlueZ has already torn down the Device1 object (a race during connection state changes).

### ~~`syncsonic_ble/state_management/device_manager.py`~~ — RESOLVED

- ~~**line 125** — `Argument 1 has incompatible type "Any | None"; expected "str"`~~
- ~~**line 136** — `Argument 1 to "_handle_new_connection" has incompatible type "Any | None"; expected "str"`~~

**Fix:** Resolve `path if path is not None else eff_path` once into `resolved_path`, bail early if both are None, then stringify into `path_str` for the `_handle_new_connection` and `_extract_mac` calls. Line 136 previously passed `path` directly which could be None when only `*args[3]` was populated — now passes the resolved value.

## Real annotation gaps (all resolved)

### ~~`syncsonic_ble/helpers/pulseaudio_helpers.py:132`~~ — RESOLVED

**Fix:** Changed `find_actual_sink_name() -> str` to `-> str | None` to match reality (it does fall through to `return None` when no sink matches the prefix). The callers already check `if actual_sink_name:` before using the result, so no behavioral change — the previous lie about return type was the only issue.

### ~~`syncsonic_ble/main.py:64`~~ — RESOLVED

`subprocess.run(["hciconfig", reserved, "name", "SyncSonic"], ...)` complained because `reserved` was typed as `str | None` (imported from `constants.reserved`, which was `os.getenv("RESERVED_HCI")` — mypy didn't recognize the post-init `if not reserved: raise RuntimeError` as proof of non-None).

**Fix:** Narrowed at the source in `constants.py`:

```python
_reserved_raw = os.getenv("RESERVED_HCI")
if not _reserved_raw:
    raise RuntimeError("RESERVED_HCI not set – cannot pick phone adapter")
reserved: str = _reserved_raw
```

The explicit `str` annotation on the post-narrow assignment propagates through every `from ... import reserved` site, fixing this error without any change at the call site.

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

## How to fix — historical (kept for reference)

All 12 baseline errors are resolved as of the 2026-05-14 narrowing pass. The remaining gradual-adoption step is flipping `check_untyped_defs = true` in `pyproject.toml` and addressing the implicit-`Any` function bodies that flag surfaces (currently informational `[annotation-unchecked]` notes, not errors). That's an isolated decision now — no real bugs need fixing first.

## Not in CI yet

Mypy is **not yet wired into `.github/workflows/ci.yml`**. Two ways to add it:

- **Hard fail on any error** — would block PRs today because of the 20 baseline errors. Bad without first fixing them.
- **Run mypy but allow failure** (`continue-on-error: true`) — runs the audit on every PR, captures regressions as a non-blocking signal. Reasonable interim.
- **Hard fail with `# type: ignore[<code>]` baseline** — mypy will only fail on *new* errors. Cleanest, but requires writing those ignores or generating a baseline file. mypy's `--baseline` flag (or a similar wrapper) makes this manageable.

Recommend the second option as the immediate next slice — gives operator-visible regression detection without breaking PR throughput.
