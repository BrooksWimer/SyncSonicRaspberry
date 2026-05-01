import { useEffect, useState, useCallback, useRef, createContext } from "react";
import { Alert, Platform } from "react-native";
import { PERMISSIONS, request, requestMultiple } from "react-native-permissions";
import * as ExpoDevice from "expo-device";
import {
  BleError,
  BleManager,
  Characteristic,
  Device,
} from "react-native-ble-plx";
import {
  SERVICE_UUID,
  CHARACTERISTIC_UUID,
  MESSAGE_TYPES,
  CoordinatorState,
  CoordinatorEvent,
  CalibrationResultPayload,
} from "@/utils/ble_constants";

import { getConfigurations, getSpeakers, updateConnectionStatus, updateSpeakerConnectionStatus } from "@/utils/database";

type NotificationHandler = (error: BleError | null, characteristic: Characteristic | null) => void;

interface ConnectionStatus {
  status: string;
  progress?: number;
  error?: string;
  mac?: string;
}

interface BLEContextType {
  allDevices: Device[];
  connectedDevice: Device | null;
  isScanning: boolean;
  requestPermissions: () => Promise<boolean>;
  manager: BleManager;
  waitForPi: () => Promise<Device>;
  ensurePiNotifications: (dev: Device, onNotify: (e: BleError | null, c: Characteristic | null) => void) => void;
  handleNotification: NotificationHandler;
  connectionStatus: ConnectionStatus | null;
  setConnectionStatus: (status: ConnectionStatus | null) => void;
  clearConnectionStatus: () => void;
}

const BLEContext = createContext<BLEContextType | null>(null);

function updateDatabaseConnectionStates(connectedMacs: string[], onUpdate?: () => void) {
  console.log("[BLE] 🔄 Updating DB connection states...");
  console.log("[BLE] ✅ Connected MACs:", connectedMacs);

  const configs = getConfigurations();
  let didUpdate = false;

  for (const config of configs) {
    console.log(`[BLE] 📦 Checking config "${config.name}" (ID: ${config.id})`);
    const speakers = getSpeakers(config.id);
    let anySpeakerConnected = false;

    for (const speaker of speakers) {
      const mac = speaker.mac?.toUpperCase();
      const isNowConnected = connectedMacs.includes(mac);
      const wasConnected = speaker.is_connected === 1;

      console.log(`[BLE] 🔍 Speaker: ${speaker.name} [${mac}] → wasConnected=${wasConnected}, isNowConnected=${isNowConnected}`);

      if (wasConnected !== isNowConnected) {
        console.log(`[BLE] ✏️ Updating speaker "${speaker.name}" → is_connected=${isNowConnected ? 1 : 0}`);
        updateSpeakerConnectionStatus(config.id, mac, isNowConnected);
        didUpdate = true;
      }

      if (isNowConnected) anySpeakerConnected = true;
    }

    const configShouldBe = anySpeakerConnected ? 1 : 0;
    if (config.isConnected !== configShouldBe) {
      console.log(`[BLE] ⚙️ Updating config "${config.name}" → isConnected=${configShouldBe}`);
      updateConnectionStatus(config.id, configShouldBe);
      didUpdate = true;
    } else {
      console.log(`[BLE] ↪️ Config "${config.name}" already correct`);
    }
  }

  if (didUpdate) {
    console.log("[BLE] ✅ DB updated, triggering UI refresh");
    if (onUpdate) onUpdate();
  } else {
    console.log("[BLE] 🚫 No changes detected in DB");
  }
}

export function useBLE(onNotification?: NotificationHandler) {
  const [bleManager] = useState(() => new BleManager({
    restoreStateIdentifier: 'sync-sonic-ble',
    restoreStateFunction: (restoredState) => {
      console.log('BLE Manager state restored:', restoredState);
    }
  }));

  const [allDevices, setAllDevices] = useState<Device[]>([]);
  const [connectedDevice, setConnectedDevice] = useState<Device | null>(null);
  const [pendingDevices, setPendingDevices] = useState<Device[]>([]);
  const [updateTimeout, setUpdateTimeout] = useState<NodeJS.Timeout | null>(null);
  const [isScanning, setIsScanning] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus | null>(null);
  const [scannedDevices, setScannedDevices] = useState<Array<{ mac: string; name: string; paired?: boolean }>>([]);
  const [wifiScannedDevices, setWifiScannedDevices] = useState<Array<{ mac: string; name: string; paired?: boolean }>>([]);
  const [pairedDevices, setPairedDevices] = useState<Array<{ mac: string; name: string; paired?: boolean }>>([]);

  // Slice 3.6: live Coordinator state pushed at 1 Hz over BLE. Latest
  // snapshot only - subscribers re-render on each tick. The periodic
  // push intentionally does NOT log to console (1 Hz would spam),
  // matching the journal-hygiene fix on the backend side.
  const [coordinatorState, setCoordinatorState] = useState<CoordinatorState | null>(null);
  // Edge-triggered Coordinator events (soft-mute, recovery). Bounded
  // ring buffer; oldest events are dropped past the limit.
  const COORDINATOR_EVENT_BUFFER_SIZE = 50;
  const [coordinatorEvents, setCoordinatorEvents] = useState<CoordinatorEvent[]>([]);

  const CALIBRATION_EVENT_BUFFER_SIZE = 80;
  const [calibrationEvents, setCalibrationEvents] = useState<CalibrationResultPayload[]>([]);

  const clearConnectionStatus = useCallback(() => {
    setConnectionStatus(null);
  }, []);

  // Stable notification handler (useCallback with empty deps because it only
  // closes over React state *setters*, which are guaranteed stable across
  // renders by React. Without useCallback, every BLEProvider re-render
  // (triggered by any state change like wifiScannedDevices updating) creates
  // a new function reference. Screens that have `handleNotification` in their
  // useEffect deps then re-run the effect, re-calling clearWifiScannedDevices
  // and wiping the scan results immediately after they arrive.
  const handleNotification: NotificationHandler = useCallback((error, characteristic) => {
    if (error) {
      // Check if the error is a disconnection
      if (error.message?.includes('disconnected')) {
        Alert.alert(
          "Disconnected",
          "Phone connection was lost.",
          [{ text: "OK" }]
        );
      }
      return;
    }

    if (!characteristic?.value) {
      console.warn("[BLE] Empty notification received");
      return;
    }

    try {
      const rawBytes = atob(characteristic.value);

      if (rawBytes.length < 2) {
        console.warn("[BLE] Notification payload too short");
        return;
      }

      const opcode = rawBytes.charCodeAt(0);      // first byte
      const jsonString = rawBytes.slice(1);        // rest is JSON
      let payload: any;
      try {
        payload = JSON.parse(jsonString);
      } catch {
        // BlueZ silently truncates ATT notifications larger than the
        // negotiated MTU (~672 bytes -> 669 byte payload). The Pi
        // sometimes emits sequence-level CALIBRATION_RESULT events
        // whose serialised JSON exceeds this (the 'sequence_complete'
        // event embeds the full nested anchor_info block, easily
        // ~750 bytes). Don't surface this as a red error in dev mode;
        // log a warn and synthesize a stub event for the calibration
        // UI so SpeakerConfigScreen can still detect a sequence
        // completion via per-speaker events.
        console.warn(
          "[BLE] Notification JSON truncated (opcode=0x" +
            opcode.toString(16) + ", rawLength=" + rawBytes.length + ")",
        );
        if (opcode === MESSAGE_TYPES.CALIBRATION_RESULT) {
          const stub = {
            phase: "notification_truncated",
            _raw_length: rawBytes.length,
          } as unknown as CalibrationResultPayload;
          setCalibrationEvents((prev) => {
            const next = [...prev, stub];
            return next.length > CALIBRATION_EVENT_BUFFER_SIZE
              ? next.slice(next.length - CALIBRATION_EVENT_BUFFER_SIZE)
              : next;
          });
        }
        return;
      }

      // Slice 3.6: COORDINATOR_STATE arrives at 1 Hz; logging it
      // would flood the JS console. Edge-triggered events and all
      // other opcodes still log normally.
      if (
        opcode !== MESSAGE_TYPES.COORDINATOR_STATE &&
        opcode !== MESSAGE_TYPES.CALIBRATION_RESULT
      ) {
        console.log("[BLE] Received notification:", { opcode, payload });
      }

      switch (opcode) {
        case MESSAGE_TYPES.SUCCESS:
          if (payload.scanning !== undefined) {
            // ACK for SCAN_START/STOP – nothing to do
          } else if (payload.connected) {
            // List of currently connected speakers
            updateDatabaseConnectionStates(payload.connected);
          } else if (payload.ultrasonic_sync_done) {
            // Ultrasonic sync result – handled by BLEContext handleNotify
          } else {
            // Assume this is the paired-device list coming from GET_PAIRED_DEVICES (0x64)
            const list: { mac: string; name: string; paired?: boolean }[] =
              Object.entries(payload).map(([mac, name]) => ({
                mac,
                name: name as string,
                paired: true,
              }));
            setPairedDevices(list);
          }
          break;

        case MESSAGE_TYPES.SCAN_DEVICES: {
            // payload.device === { mac, name, paired }
            const dev = payload.device as { mac: string; name: string; paired: boolean };
            setScannedDevices(old => {
              if (old.find(d => d.mac === dev.mac)) return old;
              return [...old, { mac: dev.mac, name: dev.name }];
            });
            break;
          }

        case MESSAGE_TYPES.WIFI_SCAN_RESULTS: {
            // Two payload shapes are supported:
            //   1. { device: { device_id, name, ip, type } } - one device per notification
            //      (matches SCAN_DEVICES streaming style; the Pi backend streams
            //      results as they're discovered).
            //   2. { phase: 'complete', count: N } - terminal marker after the last device.
            //   3. { wifi_devices: [...] } - legacy bulk shape, still tolerated.
            if (payload.phase === 'complete') {
              break;
            }
            const dev = payload.device as
              | { device_id: string; name?: string; ip?: string; type?: string }
              | undefined;
            if (dev && dev.device_id) {
              setWifiScannedDevices(old => {
                if (old.find(d => d.mac === dev.device_id)) return old;
                return [...old, { mac: dev.device_id, name: dev.name || 'Sonos' }];
              });
              break;
            }
            const list = (payload.wifi_devices || []) as Array<{ device_id: string; name: string; ip?: string; type?: string }>;
            if (Array.isArray(list) && list.length > 0) {
              setWifiScannedDevices(list.map(d => ({ mac: d.device_id, name: d.name || 'Sonos' })));
            }
            break;
          }

        case MESSAGE_TYPES.CONNECTION_STATUS_UPDATE:
          // Handle connection status update
          if (payload.phase) {
            let statusMessage = "";
            let progress = undefined;
            let error = undefined;

            switch (payload.phase) {
              case "fsm_start":
                statusMessage = "Starting connection process...";
                break;
              case "fsm_state":
                statusMessage = `Connecting (${payload.state})...`;
                progress = (payload.attempt / 3) * 100;
                break;
              case "discovery_start":
                statusMessage = "Place Speaker in Pairing Mode";
                break;
              case "discovery_complete":
                statusMessage = "Speaker found!";
                break;
              case "pairing_start":
                statusMessage = "Pairing with speaker...";
                break;
              case "pairing_success":
                statusMessage = "Pairing successful!";
                break;
              case "trusting":
                statusMessage = "Establishing trust...";
                break;
              case "connect_start":
                statusMessage = "Connecting to speaker...";
                break;
              case "connect_success":
                statusMessage = "Connection successful!";
                break;
              case "discovery_timeout":
                statusMessage = "Could not find speaker. Please ensure it's in pairing mode.";
                error = statusMessage;
                break;
              case "pairing_failed":
                statusMessage = `Pairing failed (attempt ${payload.attempt}/3). Please try again.`;
                error = statusMessage;
                break;
              case "connect_failed":
                statusMessage = `Connection failed (attempt ${payload.attempt}/3). Please try again.`;
                error = statusMessage;
                break;
              case "connect_profile_failed":
                statusMessage = "Profile connection failed. Please try again.";
                error = statusMessage;
                break;
              case "loopback creation failed":
                statusMessage = "Connection successful but audio routing failed. Please try connecting again.";
                error = statusMessage;
                break;
            }

            setConnectionStatus({
              status: statusMessage,
              progress: progress,
              error: error,
              mac: payload.device
            });
          }
          break;
        
        case MESSAGE_TYPES.COORDINATOR_STATE: {
          // Slice 3.6: 1 Hz audio-engine health snapshot. Just stash
          // the latest one in state; UI components subscribe via the
          // returned `coordinatorState` and re-render.
          setCoordinatorState(payload as CoordinatorState);
          break;
        }

        case MESSAGE_TYPES.COORDINATOR_EVENT: {
          // Edge-triggered (soft_mute fire / recover). Push into a
          // bounded ring buffer so app screens can render a timeline.
          const evt = payload as CoordinatorEvent;
          console.log(
            `[Coordinator] ${evt.phase.toUpperCase()} ${evt.mac}` +
            ` (reason=${evt.reason}` +
            (evt.rssi_dbm !== undefined ? `, rssi=${evt.rssi_dbm}dBm` : "") +
            (evt.rssi_dip_db !== undefined ? `, dip=${evt.rssi_dip_db}dB` : "") +
            `)`,
          );
          setCoordinatorEvents(prev => {
            const next = [...prev, evt];
            return next.length > COORDINATOR_EVENT_BUFFER_SIZE
              ? next.slice(next.length - COORDINATOR_EVENT_BUFFER_SIZE)
              : next;
          });
          break;
        }

        case MESSAGE_TYPES.CALIBRATION_RESULT: {
          const cal = payload as CalibrationResultPayload;
          setCalibrationEvents((prev) => {
            const next = [...prev, cal];
            return next.length > CALIBRATION_EVENT_BUFFER_SIZE
              ? next.slice(next.length - CALIBRATION_EVENT_BUFFER_SIZE)
              : next;
          });
          const ph = String(cal.phase ?? "");
          if (
            ph === "applied" ||
            ph === "failed" ||
            ph.startsWith("sequence")
          ) {
            console.log("[Calibration]", ph, cal.mac ?? "", cal);
          }
          break;
        }

        case MESSAGE_TYPES.ERROR:
          // Handle error messages
          if (payload.phase) {
            let errorMessage = "";
            
            switch (payload.phase) {
              case "discovery_timeout":
                errorMessage = "Could not find speaker. Please ensure it's in pairing mode.";
                break;
              case "pairing_failed":
                errorMessage = `Pairing failed (attempt ${payload.attempt}/3). Please try again.`;
                break;
              case "connect_failed":
                errorMessage = `Connection failed (attempt ${payload.attempt}/3). Please try again.`;
                break;
              case "loopback creation failed":
                errorMessage = "Connection successful but audio routing failed. Please try connecting again.";
                break;
              default:
                errorMessage = `Error: ${payload.phase}`;
            }

            setConnectionStatus({
              status: "Connection failed",
              error: errorMessage,
              mac: payload.device
            });
          }
          break;
        
        default:
          console.warn(`[BLE] Unexpected opcode: ${opcode}`);
      }

    } catch (err) {
      // Don't surface as a red error: the most common cause is a
      // truncated/malformed BLE notification, which we already handle
      // explicitly above. Anything reaching here is non-fatal — the
      // notification stream stays alive for the next event.
      console.warn("[BLE] Failed to decode notification:", err);
    }
  }, []); // empty deps: only closes over stable setState functions

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (updateTimeout) {
        clearTimeout(updateTimeout);
      }
      if (isScanning) {
        bleManager.stopDeviceScan();
      }
      bleManager.destroy();
    };
  }, [isScanning, updateTimeout]);

  const batchUpdateDevices = () => {
    if (pendingDevices.length > 0) {
      setAllDevices(prev => {
        const newDevices = pendingDevices.filter(newDevice => 
          !prev.some(existingDevice => existingDevice.id === newDevice.id)
        );
        return [...prev, ...newDevices];
      });
      setPendingDevices([]);
    }
  };

  // Track which devices we've already attached a notification monitor
  // to. Without this every consumer re-render that re-runs an effect
  // depending on `ensurePiNotifications` (which is recreated each
  // render of the BLE provider) would attach a NEW monitor on top of
  // the existing one, exponentially multiplying the JS work per
  // incoming notification and turning the app into a slideshow once
  // the Coordinator's 1 Hz push starts arriving.
  // The previous guard checked `dev.monitorListeners?.length`, but on
  // iOS BLE-plx that property is not reliable across reconnect cycles.
  const monitoredDeviceIdsRef = useRef<Set<string>>(new Set());
  const monitorSubscriptionsRef = useRef<Map<string, { remove: () => void }>>(new Map());

  const ensurePiNotifications = useCallback(
    async (
      dev: Device,
      onNotify: (e: BleError | null, c: Characteristic | null) => void
    ) => {
      if (monitoredDeviceIdsRef.current.has(dev.id)) return;
      monitoredDeviceIdsRef.current.add(dev.id);

      try {
        console.log('[BLE] discovering SVC/CHR for', dev.id);
        const d2 = await dev.discoverAllServicesAndCharacteristics();
        const svcs = await d2.services();
        const svc = svcs.find(
          s => s.uuid.toLowerCase() === SERVICE_UUID.toLowerCase()
        );
        if (!svc) throw new Error('Pi service not found');

        const chrs = await svc.characteristics();
        const chr = chrs.find(
          c => c.uuid.toLowerCase() === CHARACTERISTIC_UUID.toLowerCase()
        );
        if (!chr) throw new Error('Pi characteristic not found');

        console.log('[BLE] enabling notifications …');
        const sub = chr.monitor((err, c) => {
          if (err) console.error('[BLE] monitor error:', err);
          onNotify(err, c);
        });
        monitorSubscriptionsRef.current.set(dev.id, sub);
      } catch (e) {
        // Allow a future retry on failure - clearing the marker so the
        // next call can try again rather than silently doing nothing.
        monitoredDeviceIdsRef.current.delete(dev.id);
        throw e;
      }
    },
    []
  );

  const isDuplicateDevice = (devices: Device[], nextDevice: Device) =>
    devices.findIndex((device) => nextDevice.id === device.id) > -1;

  const scanForPeripherals = () => {
    console.log('Starting BLE scan...');
    
    // Stop any existing scan
    bleManager.stopDeviceScan();
    
    // Clear any pending updates
    if (updateTimeout) {
      clearTimeout(updateTimeout);
    }
    setPendingDevices([]);
    
    // Simple scanning options that worked in the old version
    const scanOptions = {
      allowDuplicates: false,
      scanMode: 2, // SCAN_MODE_LOW_LATENCY
    };
    
    console.log('Using scan options:', scanOptions);
    
    bleManager.startDeviceScan(
      null,
      scanOptions,
      async (error, device) => {
        if (error) {
          console.error('BLE scan error:', error);
          return;
        }
        if (device) {
          try {
            // Log device details
            console.log('Found device:', {
              id: device.id,
              name: device.name,
              localName: device.localName,
              rssi: device.rssi,
              serviceUUIDs: device.serviceUUIDs
            });
            
            // Only add devices with names
            if (device.name || device.localName) {
              // Add to pending devices if not already present
              setPendingDevices(prev => {
                if (!prev.some(d => d.id === device.id)) {
                  const newDevices = [...prev, device];
                  // Update allDevices immediately with new devices
                  setAllDevices(current => {
                    const existingIds = new Set(current.map(d => d.id));
                    const newDevicesToAdd = newDevices.filter(d => !existingIds.has(d.id));
                    return [...current, ...newDevicesToAdd];
                  });
                  return newDevices;
                }
                return prev;
              });
            }
          } catch (e) {
            console.error('Error processing device:', e);
          }
        }
      }
    );
  };

  

  const stopScan = () => {
    console.log('Stopping BLE scan...');
    if (updateTimeout) {
      clearTimeout(updateTimeout);
      batchUpdateDevices(); // Process any remaining pending devices
    }
    bleManager.stopDeviceScan();
  };

  const connectToDevice = async (device: Device) => {
    try {
      console.log('Connecting to device:', device.id);
      
      // Stop scanning before attempting to connect
      if (isScanning) {
        console.log('Stopping scan before connecting...');
        stopScan();
        setIsScanning(false);
        
        // Add a small delay to allow the BLE stack to clean up
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      
      // Ensure scanning is stopped
      bleManager.stopDeviceScan();
      
      const deviceConnection = await bleManager.connectToDevice(device.id, {
        timeout: 10000, // 10 second timeout
        autoConnect: false // Don't auto-connect
      });
      
      setConnectedDevice(deviceConnection);

      // When the Pi link drops, purge the monitor-id marker so the
      // next reconnect can re-attach a fresh listener (the previous
      // monitor subscription is dead and cannot be reused).
      bleManager.onDeviceDisconnected(deviceConnection.id, () => {
        monitoredDeviceIdsRef.current.delete(deviceConnection.id);
        const sub = monitorSubscriptionsRef.current.get(deviceConnection.id);
        if (sub) {
          try { sub.remove(); } catch { /* sub already gone */ }
          monitorSubscriptionsRef.current.delete(deviceConnection.id);
        }
      });

      // Discover services and characteristics
      await deviceConnection.discoverAllServicesAndCharacteristics();
      // 1) fetch the services array
      const services = await deviceConnection.services()

      // 2) log out exactly what you got back
      console.log("Discovered services on Pi:", services.map(s => s.uuid))

      // now you can proceed to monitor/ write…
      if (!services.some(s => s.uuid === SERVICE_UUID)) {
        console.log("🛑 Our custom service UUID not found!")
}

      // Set up notification handler if provided. Mark the device as
      // monitored so the consumer-side ``ensurePiNotifications`` (called
      // from screen useEffects) is a no-op and does not stack a second
      // listener on top of this one.
      if (onNotification) {
        await deviceConnection.monitorCharacteristicForService(
          SERVICE_UUID,
          CHARACTERISTIC_UUID,
          (err, char) => {
            handleNotification(err, char);
            onNotification?.(err, char);
          }
        );
        monitoredDeviceIdsRef.current.add(deviceConnection.id);
      }

      return deviceConnection;
    } catch (e) {
      console.error("Failed to connect:", e);
      throw e;
    }
  };

  const requestPermissions = async () => {
    if (Platform.OS === 'android') {
      const apiLevel = ExpoDevice.platformApiLevel;
      if (apiLevel === null) {
        console.error('Could not determine Android API level');
        return false;
      }
      
      if (apiLevel < 31) {
        const result = await request(PERMISSIONS.ANDROID.ACCESS_FINE_LOCATION);
        return result === 'granted';
      } else {
        const results = await requestMultiple([
          PERMISSIONS.ANDROID.BLUETOOTH_SCAN,
          PERMISSIONS.ANDROID.BLUETOOTH_CONNECT,
          PERMISSIONS.ANDROID.ACCESS_FINE_LOCATION,
        ]);
        return (
          results[PERMISSIONS.ANDROID.BLUETOOTH_SCAN] === 'granted' &&
          results[PERMISSIONS.ANDROID.BLUETOOTH_CONNECT] === 'granted' &&
          results[PERMISSIONS.ANDROID.ACCESS_FINE_LOCATION] === 'granted'
        );
      }
    } else {
      return true;
    }
  };

  // inside useBLE (add just after stopScan)
const waitForPi = (): Promise<Device> =>
  new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      stopScan();
      reject(new Error("Scan timeout (15 s)"));
    }, 15_000);

    // narrow scan to our service UUID to reduce noise
    bleManager.startDeviceScan([SERVICE_UUID], null, (err, dev) => {
      if (err) {
        stopScan();
        clearTimeout(timer);
        return reject(err);
      }
      if (
        dev &&
        (dev.name?.toLowerCase() === "sync-sonic" ||
         dev.localName?.toLowerCase() === "sync-sonic" ||
         dev.name?.toLowerCase() === "syncsonic" ||
         dev.localName?.toLowerCase() === "SyncSonic")
      ) {
        stopScan();
        clearTimeout(timer);
        resolve(dev);
      }
    });
  });

  return {
    scanForPeripherals,
    stopScan,
    connectToDevice,
    allDevices,
    connectedDevice,
    isScanning,
    requestPermissions,
    manager: bleManager,
    waitForPi,
    ensurePiNotifications,      
    handleNotification,
    connectionStatus,
    setConnectionStatus,
    clearConnectionStatus,
    scannedDevices,
    wifiScannedDevices,
    clearWifiScannedDevices: () => setWifiScannedDevices([]),
    pairedDevices,
    // Slice 3.6: audio-engine policy state surfaced from the Pi.
    coordinatorState,
    coordinatorEvents,
    clearCoordinatorEvents: () => setCoordinatorEvents([]),
    calibrationEvents,
    clearCalibrationEvents: () => setCalibrationEvents([]),
  };
}

export default useBLE;