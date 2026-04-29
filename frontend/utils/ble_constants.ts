// BLE Constants
export const BLE_DEVICE_NAME = "Sync-Sonic";
export const SERVICE_UUID = "19b10000-e8f2-537e-4f6c-d104768a1214";
export const CHARACTERISTIC_UUID = "19b10001-e8f2-537e-4f6c-d104768a1217";

// Message Types (must match backend)
export const MESSAGE_TYPES = {
  // Ping/Pong
  PING: 0x01,
  PONG: 0x02,
  ERROR: 0x03,
  // Response types
  SUCCESS: 0xF0,
  FAILURE: 0xF1,
  // Connection related
  CONNECT_ONE: 0x60,
  DISCONNECT: 0x61,
  // Volume and latency
  SET_LATENCY: 0x62,
  SET_VOLUME: 0x63,
  // Device management
  GET_PAIRED_DEVICES: 0x64,
  // Mute control
  SET_MUTE: 0x65,
  START_CLASSIC_PAIRING: 0x66,
  // Ultrasonic auto-sync (Pi runs one sync cycle; result via notification)
  ULTRASONIC_SYNC: 0x67,
  // Connection Status Update
  CONNECTION_STATUS_UPDATE: 0x70,  // General connection status update
  // Slice 3.6: Coordinator (audio engine policy layer) telemetry
  COORDINATOR_STATE : 0x71,        // 1 Hz per-speaker health snapshot
  COORDINATOR_EVENT : 0x72,        // Edge-triggered soft-mute / state change
  SCAN_DEVICES      : 0x43,
  SCAN_START        : 0x40,
  SCAN_STOP         : 0x41,
  WIFI_SCAN_START   : 0x44,
  WIFI_SCAN_STOP    : 0x45,
  WIFI_SCAN_RESULTS : 0x46,
} as const;

// Slice 3.6: Coordinator BLE payload shapes. The backend authoritative
// definition lives in docs/maverick/proposals/05-coordinated-engine-architecture.md
// section 15. Keep these in sync with that doc + backend
// coordinator/coordinator.py (_push_ble_state / _push_ble_event).

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
