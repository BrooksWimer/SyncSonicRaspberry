# SyncSonic BLE Protocol

Wire-level reference for the GATT protocol between the React Native frontend (`frontend/utils/ble_codec.ts`, `frontend/utils/ble_constants.ts`) and the Python backend (`backend/syncsonic_ble/infra/gatt_service.py`, `backend/syncsonic_ble/state_change/action_request_handlers.py`, `backend/syncsonic_ble/utils/constants.py`).

Reverse-engineered from the live code on `main` as of 2026-05-14. **Keep this doc and `Msg` / `MESSAGE_TYPES` in sync** — any new message type or payload field should appear in three places: the Python `Msg` IntEnum, the TypeScript `MESSAGE_TYPES` const, and this doc.

## Service & Characteristic

| Name | UUID | Source |
|---|---|---|
| Service | `19b10000-e8f2-537e-4f6c-d104768a1214` | `SERVICE_UUID` in both frontends |
| Characteristic | `19b10001-e8f2-537e-4f6c-d104768a1217` | `CHARACTERISTIC_UUID` in both frontends |
| CCCD | `00002902-0000-1000-8000-00805f9b34fb` | `CCCD_UUID` (Bluetooth standard) |

A single characteristic carries every message in both directions:

- **Writes** from mobile to Pi → request handlers in `action_request_handlers.HANDLERS`.
- **Notifications** from Pi to mobile → `Characteristic.send_notification(...)` paired with the mobile-side notification subscription (CCCD set to 0x0001).

The advertised device name is `Sync-Sonic` (`BLE_DEVICE_NAME` in the frontend).

## Wire format

Every payload, in either direction, is:

```
+----+----------------------------------+
| TT |       JSON body (UTF-8)          |
+----+----------------------------------+
| 1B |  variable length                 |
+----+----------------------------------+
```

then base64-encoded for transport over the GATT characteristic.

- **`TT`** — single byte, message type. The full set lives in `Msg` (backend `IntEnum`) and `MESSAGE_TYPES` (frontend `as const`).
- **JSON body** — UTF-8-encoded JSON document. May be empty `{}` for messages with no payload (e.g. `PING` with no count). The body always includes whatever fields the handler / consumer expects; see the per-message tables below.

### Frontend encoder / decoder

[`frontend/utils/ble_codec.ts`](../frontend/utils/ble_codec.ts):

```ts
export function encode(type: number, data: any = {}): string {
  const json = JSON.stringify(data);
  // Sized by UTF-8 byte length (fixed in PR #7) so non-ASCII payloads
  // — speaker nicknames with accents/emoji, calibration phase strings
  // — survive the encoder without RangeError.
  const jsonBytes = new TextEncoder().encode(json);
  const bytes = new Uint8Array(1 + jsonBytes.length);
  bytes[0] = type;
  bytes.set(jsonBytes, 1);
  return btoa(String.fromCharCode(...bytes));
}

export function decode(b64: string) {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const type  = bytes[0];
  const json  = bytes.length > 1
    ? JSON.parse(new TextDecoder().decode(bytes.slice(1)))
    : {};
  return { type, json };
}
```

### Backend encoder

[`backend/syncsonic_ble/state_change/action_request_handlers.py`](../backend/syncsonic_ble/state_change/action_request_handlers.py):

```python
def _encode(msg: Msg, payload: Dict[str, Any]):
    raw = json.dumps(payload).encode()
    return [dbus.Byte(msg)] + [dbus.Byte(byte) for byte in raw]
```

The result is a list of `dbus.Byte`s that BlueZ wraps into the characteristic value bytes. The mobile side receives them as a base64-encoded blob via `react-native-ble-plx`.

## Request types (mobile → Pi)

These are written to the characteristic by the mobile app. Each one is dispatched by `HANDLERS` to a `handle_*` function in `action_request_handlers.py`. The handler returns an `_encode(Msg.<RESPONSE_TYPE>, payload)` byte list; the mobile reads it back from the same characteristic.

| Type | Value | Handler | Body | Response |
|---|---|---|---|---|
| `PING` | `0x01` | `handle_ping` | `{count?: number}` | `PONG {count}` |
| `SCAN_START` | `0x40` | `_scan_start` | `{}` (some flows include filter knobs) | `SUCCESS` + later `SCAN_DEVICES` notifications |
| `SCAN_STOP` | `0x41` | `_scan_stop` | `{}` | `SUCCESS` |
| `WIFI_SCAN_START` | `0x44` | `handle_wifi_scan_start` | `{}` | `SUCCESS` + later `WIFI_SCAN_RESULTS` |
| `WIFI_SCAN_STOP` | `0x45` | `handle_wifi_scan_stop` | `{}` | `SUCCESS` |
| `CONNECT_ONE` | `0x60` | `handle_connect_one` | `{targetSpeaker: {mac, name?}, allowed?: string[], settings?: Record<mac, {latency, volume, ...}>}` | `SUCCESS {queued: true}` then asynchronous `CONNECTION_STATUS_UPDATE` notifications |
| `DISCONNECT` | `0x61` | `handle_disconnect` | `{mac}` | `SUCCESS {queued: true}` |
| `SET_LATENCY` | `0x62` | `handle_set_latency` | `{mac, latency}` (latency in ms) | `SUCCESS {latency, transport}` or `ERROR` |
| `SET_VOLUME` | `0x63` | `handle_set_volume` | `{mac, volume}` (volume 0-100) | `SUCCESS {volume, transport}` or `ERROR` |
| `GET_PAIRED_DEVICES` | `0x64` | `handle_get_paired` | `{}` | `SUCCESS {paired_devices}` |
| `SET_MUTE` | `0x65` | `handle_set_mute` | `{mac, mute: boolean}` | `SUCCESS` or `ERROR` |
| `ULTRASONIC_SYNC` | `0x67` | `handle_ultrasonic_sync` | `{}` | `SUCCESS` + later `CALIBRATION_RESULT` notifications |
| `CALIBRATE_SPEAKER` | `0x68` | `handle_calibrate_speaker` | `{mac, calibration_mode?}` (Slice 4.2) | `SUCCESS` + later `CALIBRATION_RESULT` notifications |
| `CALIBRATE_ALL_SPEAKERS` | `0x69` | `handle_calibrate_all_speakers` | `{}` (Slice 4.3) | `SUCCESS` + sequential per-output `CALIBRATION_RESULT` notifications |

### Common error envelope

Every request handler returns `_encode(Msg.ERROR, {...})` for validation failures or transport-layer issues. The body shape:

```json
{
  "error": "<human-readable cause>",
  "<context fields specific to the failure>": "..."
}
```

Examples:
- `{"error": "Missing targetSpeaker.mac"}`
- `{"error": "Invalid latency value"}`
- `{"error": "MAC is on the reserved adapter (phone), cannot apply output delay"}`
- `{"error": "stage delay update failed"}`

If a feature is disabled at runtime, the handler returns:

```json
{
  "error": "<message>",
  "feature_disabled": true,
  "feature": "<feature key>"
}
```

## Notification types (Pi → mobile)

These are pushed by the Pi via `Characteristic.send_notification(...)` outside of any explicit request. The mobile listens via the standard CCCD subscription.

| Type | Value | Body | Trigger |
|---|---|---|---|
| `PONG` | `0x02` | `{count}` | Response to `PING` |
| `ERROR` | `0x03` | `{error, ...}` | Validation / runtime failure in any request handler |
| `SUCCESS` | `0xF0` | varies (`{queued, ...}`, `{latency, transport}`, ...) | Generic positive ack for a successful request |
| `FAILURE` | `0xF1` | `{error?, ...}` | Generic negative ack distinct from `ERROR` (reserved for transport-level negative ack) |
| `SCAN_DEVICES` | `0x43` | `{device: {mac, name?, rssi?, ...}}` | Per-device push during an active scan (one notification per newly-seen device). Source: `device_manager.py` `send_notification(Msg.SCAN_DEVICES, ...)`. |
| `WIFI_SCAN_RESULTS` | `0x46` | `{networks: [...]}` | Push at end of Wi-Fi scan with the collected network list |
| `CONNECTION_STATUS_UPDATE` | `0x70` | `{mac, status: "connected"\|"disconnected"\|..., reason?, ...}` | Edge-triggered when a speaker's BlueZ `Connected` property flips, or when the coordinator's connection-status pipeline emits a phase change |
| `COORDINATOR_STATE` | `0x71` | `{tick, n_speakers, speakers: [{mac, health, gain, rssi_dbm, rssi_dip_db, delay_samples}, ...]}` | 1 Hz per-speaker health snapshot from the coordinator (Slice 3.6). Backend authoritative source: `_push_ble_state` in `coordinator/coordinator.py`. |
| `COORDINATOR_EVENT` | `0x72` | `{type: "soft_mute", phase: "mute"\|"unmute", mac, reason, ramp_ms, rssi_dbm?, rssi_dip_db?}` | Edge-triggered soft-mute / state-change from the coordinator (Slice 3.6). Backend authoritative source: `_push_ble_event`. |
| `CALIBRATION_RESULT` | `0x73` | `{phase: "<phase-name>", ...phase-specific fields}` | Async per-phase progress + final result for `ULTRASONIC_SYNC`, `CALIBRATE_SPEAKER`, `CALIBRATE_ALL_SPEAKERS` (Slice 4.x). Many notifications per run. |

### CoordinatorState shape (TypeScript mirror)

[`frontend/utils/ble_constants.ts`](../frontend/utils/ble_constants.ts) defines the interface the mobile uses to decode `COORDINATOR_STATE` bodies:

```ts
export type CoordinatorSpeakerHealth = "healthy" | "muted" | "stressed";

export interface CoordinatorSpeakerState {
  mac: string;
  health: CoordinatorSpeakerHealth;
  gain: number;          // current_gain_x1000 (1000 = full volume)
  rssi_dbm: number;      // latest sample, NOT median
  rssi_dip_db: number;   // median_60s - median_10s; positive = degrading
  delay_samples: number;
}

export interface CoordinatorState {
  tick: number;
  n_speakers: number;
  speakers: CoordinatorSpeakerState[];
}
```

### CoordinatorEvent shape

```ts
export type CoordinatorEventReason =
  | "frames_in_flowing_out_starved"
  | "rssi_dip"
  | "frames_out_recovered";

export interface CoordinatorEvent {
  type: "soft_mute";
  phase: "mute" | "unmute";
  mac: string;
  reason: CoordinatorEventReason;
  ramp_ms: number;
  rssi_dbm?: number;
  rssi_dip_db?: number;
}
```

The authoritative source for both shapes is `docs/maverick/proposals/05-coordinated-engine-architecture.md` section 15. The TS types must stay in sync with the backend's `_push_ble_state` and `_push_ble_event` emit code in `coordinator/coordinator.py`.

## Known drift / gaps

These are wire-level inconsistencies between the frontend and backend as of 2026-05-14 — not bugs to fix in this doc, but things to know about when adding new messages or debugging unexpected behavior.

### `START_CLASSIC_PAIRING` (0x66) — frontend-only

The TypeScript `MESSAGE_TYPES` declares `START_CLASSIC_PAIRING: 0x66` but the backend's `Msg` IntEnum has **no entry at 0x66**, and `HANDLERS` has no handler for that value. The mobile app can write a `0x66` message, but the dispatcher in `gatt_service.py` will fall through to `_UNKNOWN_HANDLER` and likely return `ERROR` (or be silently dropped depending on the fallback path).

Two ways to resolve when the time comes:

1. **Drop it from the frontend** if classic pairing isn't intended to be a BLE-triggered flow (BlueZ usually handles classic pairing through its agent layer, not via app messages).
2. **Implement a backend handler** that accepts a target MAC and triggers the pairing agent.

### `Msg.START_CLASSIC_PAIRING` absent from the IntEnum

Symptom of the same drift — `backend/syncsonic_ble/utils/constants.py` is the source of truth for valid type bytes server-side. Add it there at the same time as adding a handler.

## Adding a new message type

1. **Backend enum** — add the new value to `Msg` in `backend/syncsonic_ble/utils/constants.py`. Match the convention: 1-byte hex literal, comment explaining what slice/feature introduced it.
2. **Backend handler** — if it's a request, write a `handle_<thing>` function in `state_change/action_request_handlers.py` and register it in `HANDLERS`. The handler must return `_encode(Msg.SUCCESS, ...)` or `_encode(Msg.ERROR, ...)`.
3. **Backend emitter** — if it's a notification, find the right place in the runtime to call `char.send_notification(Msg.<NEW>, payload)` (e.g., `coordinator/coordinator.py` for engine events, `device_manager.py` for scan-time signals).
4. **Frontend constant** — add it to `MESSAGE_TYPES` in `frontend/utils/ble_constants.ts` with the same hex value.
5. **Frontend type** — if the body has structure, add an interface to `ble_constants.ts` next to `CoordinatorState` / `CoordinatorEvent`.
6. **Frontend caller / handler** — write or update the code that calls `encode(MESSAGE_TYPES.<NEW>, body)` for requests, or matches against the type byte in the notification callback for pushes.
7. **This doc** — update the right table above with the type, value, body shape, and trigger / response.
8. **Test** — add a roundtrip case to `frontend/__tests__/ble_codec.test.ts` so the new value is exercised by the protocol-level test suite.

## Cross-references

- [`frontend/utils/ble_codec.ts`](../frontend/utils/ble_codec.ts) — encode / decode
- [`frontend/utils/ble_constants.ts`](../frontend/utils/ble_constants.ts) — UUIDs, `MESSAGE_TYPES`, `CoordinatorState` / `CoordinatorEvent` types
- [`frontend/__tests__/ble_codec.test.ts`](../frontend/__tests__/ble_codec.test.ts) — unit tests covering the protocol's encode/decode roundtrip
- [`backend/syncsonic_ble/utils/constants.py`](../backend/syncsonic_ble/utils/constants.py) — `Msg` IntEnum + `SERVICE_UUID` / `CHARACTERISTIC_UUID`
- [`backend/syncsonic_ble/state_change/action_request_handlers.py`](../backend/syncsonic_ble/state_change/action_request_handlers.py) — `_encode` + `HANDLERS`
- [`backend/syncsonic_ble/infra/gatt_service.py`](../backend/syncsonic_ble/infra/gatt_service.py) — D-Bus GATT registration
- [`backend/syncsonic_ble/coordinator/coordinator.py`](../backend/syncsonic_ble/coordinator/coordinator.py) — `_push_ble_state` / `_push_ble_event` for Slice 3.6 notifications
- [`docs/maverick/proposals/05-coordinated-engine-architecture.md`](maverick/proposals/05-coordinated-engine-architecture.md) — section 15, authoritative coordinator payload definitions
