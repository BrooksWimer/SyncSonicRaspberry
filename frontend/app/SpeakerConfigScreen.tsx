import { useSearchParams } from 'expo-router/build/hooks';
import { Volume1, Volume2, VolumeX } from '@tamagui/lucide-icons'
import React, { useState, useEffect, useRef } from 'react';
import { StyleSheet, Alert, TouchableOpacity, ScrollView, View, Dimensions, Platform } from 'react-native';
import Slider from '@react-native-community/slider';
import { useRouter, useNavigation } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { 
  getConfigurationStatus,
  updateSpeakerSettings,
  updateSpeakerConnectionStatus,
  getSpeakersFull
} from '@/utils/database';
import { useTheme, useThemeName, YStack, Text, Button } from 'tamagui';
import { TopBar } from '@/components/topbar-variants/TopBar';
import { 
  handleVolumeChange,
  handleLatencyChange
} from '../utils/SpeakerFunctions';
import { useBLEContext, } from '@/contexts/BLEContext';
import {
  bleConnectOne,
  bleDisconnectOne,
  setVolume,
  setMute,
  calibrateAllSpeakers,
} from '../utils/ble_functions';
import LottieView from 'lottie-react-native';
import { Audio } from 'expo-av';
import { Header } from '@/components/texts/TitleText';
import { Body } from '@/components/texts/BodyText';


export default function SpeakerConfigScreen() {
  // Retrieve parameters from the URL
  const params = useSearchParams();
  const router = useRouter();
  const navigation = useNavigation();
  const speakersStr = params.get('speakers'); // JSON string or null
  const configNameParam = params.get('configName') || 'Unnamed Configuration';
  const configIDParam = params.get('configID'); // may be undefined for a new config
  const soundRef = useRef<Audio.Sound | null>(null);

  const playSound = async () => {
    try {
      console.log("Attempting to play sound");
      
      // First unload any existing sound
      if (soundRef.current) {
        await soundRef.current.unloadAsync();
      }

      // Create and load the new sound
      const { sound } = await Audio.Sound.createAsync(
        require('@/assets/sound/beep.wav'),
        { 
          shouldPlay: false, // Don't play immediately
          volume: 1.0, // Ensure full volume
          isMuted: false // Ensure not muted
        }
      );
      
      soundRef.current = sound;

      sound.setOnPlaybackStatusUpdate((status) => {
        console.log("Playback status update:", status);
        if (status.isLoaded && typeof status.positionMillis === 'number' && typeof status.durationMillis === 'number' && status.positionMillis >= status.durationMillis) {
          console.log("Sound finished playing, unloading");
          sound.unloadAsync();
        }
      });
      
      // Play the sound
      console.log("Starting playback");
      await sound.setVolumeAsync(1.0); // Ensure full volume
      await sound.setIsMutedAsync(false); // Ensure not muted
      await sound.playAsync();
      console.log("Sound played successfully");
    } catch (error) {
      console.error("Error playing sound:", error);
    }
  };

  const {
    dbUpdateTrigger,
    connectedDevice,
    piStatus,
    calibrationEvents,
    connectionStatus: bleConnectionStatus,
    clearConnectionStatus: bleClearConnectionStatus,
    setConnectionStatus,
  } = useBLEContext();

  // State to hold connected speakers (mapping from mac to name)
  const [connectedSpeakers, setConnectedSpeakers] = useState<{ [mac: string]: string }>({});

  // State for connection status: true means connected
  const [isConnected, setIsConnected] = useState<boolean>(false);

  // State for speaker settings (volume and latency)
  const [settings, setSettings] = useState<{ [mac: string]: { volume: number; latency: number; isConnected: boolean } }>({});

  const calNotificationCursorRef = useRef(0);

  // Sequence-completion fallback. The Pi's `sequence_complete` event
  // can exceed the BLE ATT MTU (~669 byte payload) because it embeds
  // the full nested anchor_info block, in which case BlueZ truncates
  // the notification and the frontend's JSON.parse fails. The
  // per-speaker `applied`/`failed` events fit comfortably under MTU
  // and are reliable, so we accumulate their outcomes and synthesize
  // the final alert from them when the bulky `sequence_complete` is
  // missing.
  const seqExpectedTotalRef = useRef<number>(0);
  const seqOutcomesRef = useRef<Record<string, string>>({});
  const seqAlertedRef = useRef<boolean>(false);

  // Last anchor lag observed during a calibration sequence. Drives the
  // unified slider scale below: when Wi-Fi outputs are present, every
  // slider scales to (anchor_lag * 1.2 or 4000ms, whichever is larger)
  // so relative slider positions stay meaningful across BT and Wi-Fi.
  const [latestAnchorLagMs, setLatestAnchorLagMs] = useState<number | null>(null);

  useEffect(() => {
    const events = calibrationEvents;
    // Per-speaker progress is shown inline next to each speaker card and
    // the top-level "Align all" banner shows the in-flight state. We only
    // pop a final Alert at sequence boundaries so the user gets one
    // unambiguous "done" confirmation instead of an N-way Alert stack.
    const fireCompletionAlert = (
      outcomes: Record<string, string>,
      title = 'Align all speakers',
    ) => {
      const summary = Object.keys(outcomes).length
        ? Object.entries(outcomes)
            .map(([m, st]) => `${connectedSpeakers[m] ?? m}: ${st}`)
            .join('\n')
        : 'Done.';
      Alert.alert(title, summary);
    };

    while (calNotificationCursorRef.current < events.length) {
      const ev = events[calNotificationCursorRef.current] as Record<string, unknown>;
      calNotificationCursorRef.current += 1;
      const phase = String(ev.phase ?? '');

      // Reset accumulators at the start of each new sequence. We also
      // treat the first per-speaker `started` event with index 1 as a
      // sequence boundary in case `sequence_started` itself was
      // truncated by BlueZ.
      const seqIdx = Number(ev.sequence_index);
      const seqTotal = Number(ev.sequence_total);
      if (
        phase === 'sequence_started' ||
        (phase === 'started' && seqIdx === 1)
      ) {
        seqExpectedTotalRef.current = Number.isFinite(seqTotal) ? seqTotal : 0;
        seqOutcomesRef.current = {};
        seqAlertedRef.current = false;
      }
      if (Number.isFinite(seqTotal) && seqTotal > 0) {
        seqExpectedTotalRef.current = Math.max(
          seqExpectedTotalRef.current, seqTotal,
        );
      }

      if (phase === 'anchor_measured') {
        const lag = Number(ev.anchor_lag_ms);
        if (Number.isFinite(lag) && lag > 0) {
          setLatestAnchorLagMs(lag);
          // Auto-update each Sonos card's slider to mirror the anchor
          // delay (read-only display value; backend ignores SET_LATENCY
          // for sonos: ids except as a UI persistence ack).
          setSettings(prev => {
            const next = { ...prev };
            Object.keys(prev).forEach(m => {
              if (m.toLowerCase().startsWith('sonos:')) {
                next[m] = { ...prev[m], latency: Math.round(lag) };
                if (configIDParam) {
                  try {
                    updateSpeakerSettings(
                      Number(configIDParam), m,
                      prev[m]?.volume ?? 50, Math.round(lag),
                    );
                  } catch (err) { console.warn('persist anchor latency failed', err); }
                }
              }
            });
            return next;
          });
        }
        continue;
      }

      if (phase === 'applied' || phase === 'failed') {
        const mac = typeof ev.mac === 'string' ? ev.mac : '';
        if (mac) {
          // Accumulate per-speaker outcome so we can synthesize the
          // completion alert if `sequence_complete` is later dropped
          // by MTU truncation.
          seqOutcomesRef.current = {
            ...seqOutcomesRef.current,
            [mac]: phase,
          };
        }
        if (phase === 'applied') {
          const newDelay = Number(ev.new_user_delay_ms);
          if (mac && Number.isFinite(newDelay)) {
            setSettings(prev => {
              if (!prev[mac]) return prev;
              const next = {
                ...prev,
                [mac]: { ...prev[mac], latency: Math.round(newDelay) },
              };
              if (configIDParam) {
                try {
                  updateSpeakerSettings(
                    Number(configIDParam), mac,
                    prev[mac]?.volume ?? 50, Math.round(newDelay),
                  );
                } catch (err) { console.warn('persist calibrated latency failed', err); }
              }
              return next;
            });
          }
        }
        // If this was the LAST speaker in the sequence and we already
        // know how many were expected, fire the completion alert as a
        // fallback. The authoritative `sequence_complete` event still
        // wins if it makes it through; this guarantees the user sees
        // *some* completion popup even when MTU truncation eats the
        // big sequence-level event.
        const total = seqExpectedTotalRef.current;
        const collected = Object.keys(seqOutcomesRef.current).length;
        if (
          total > 0 &&
          collected >= total &&
          !seqAlertedRef.current
        ) {
          seqAlertedRef.current = true;
          fireCompletionAlert(seqOutcomesRef.current);
        }
        continue;
      }

      if (phase === 'sequence_complete') {
        const outcomes = (ev.per_mac_outcome as Record<string, string> | undefined)
          ?? seqOutcomesRef.current;
        if (!seqAlertedRef.current) {
          seqAlertedRef.current = true;
          fireCompletionAlert(outcomes);
        }
        continue;
      }

      if (phase === 'sequence_failed') {
        if (!seqAlertedRef.current) {
          seqAlertedRef.current = true;
          Alert.alert(
            'Align all speakers',
            String(ev.reason ?? 'No calibratable outputs (connect speakers to the Pi first).'),
          );
        }
        continue;
      }

      if (phase === 'notification_truncated') {
        // The parser flagged a CALIBRATION_RESULT notification it
        // couldn't decode (BlueZ MTU truncation). Per-speaker events
        // are not affected; the completion alert is already handled
        // above via the seqOutcomesRef accumulator. Just record the
        // event for diagnostics and move on without alarming the user.
        continue;
      }

      // failed events: surface inline next to the speaker card; do not
      // fire a separate Alert for each.
    }
  }, [calibrationEvents, connectedSpeakers, configIDParam]);

  // Unified slider scale across all cards (BT + Wi-Fi). When any Wi-Fi
  // output is connected we scale every slider to the anchor envelope so
  // relative positions are preserved. BT-only stays at the original
  // 500 ms cap to keep slider granularity for fine alignment.
  const hasAnyWifi = Object.keys(connectedSpeakers).some(
    m => m.toLowerCase().startsWith('sonos:'),
  );
  const unifiedSliderMax = hasAnyWifi
    ? Math.max(4000, Math.ceil(((latestAnchorLagMs ?? 1500) * 1.2) / 100) * 100)
    : 500;

  // State for loading speakers
  const [loadingSpeakers, setLoadingSpeakers] = useState<{ 
    [mac: string]: { 
      action: 'connect' | 'disconnect' | null;
      statusMessage?: string;
      instructions?: string;
      error?: string;
      success?: boolean;
    } | null
  }>({});

  // Track which connections have been processed
  const [processedConnections, setProcessedConnections] = useState<Set<string>>(new Set());
  
  // Add state to track which disconnections have been processed
  const [processedDisconnections, setProcessedDisconnections] = useState<Set<string>>(new Set());

  // Speaker card overlay component - define inside the main component to access state and props
  const SpeakerCardOverlay = ({ mac, status }: { 
    mac: string, 
    status: { 
      action: 'connect' | 'disconnect' | null, 
      statusMessage?: string, 
      instructions?: string, 
      error?: string, 
      success?: boolean 
    } 
  }) => {
    const themeName = useThemeName();
    
    if (!status || !status.action) return null;
    
    // Make overlay more opaque - reduced transparency
    const bgColor = themeName === 'dark' ? 'rgba(37, 0, 71, 0.85)' : 'rgba(242, 232, 255, 0.85)';
    const textColor = themeName === 'dark' ? '#F2E8FF' : '#26004E';
    
    // Use the same Lottie animation sources as ConnectionStatusOverlay
    const loaderSource = themeName === 'dark'
      ? require('../assets/animations/SyncSonic_Loading_Dark_nbg.json')
      : require('../assets/animations/SyncSonic_Loading_Light_nbg.json');
    
    return (
      <View style={{
        position: 'absolute',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        backgroundColor: bgColor,
        justifyContent: 'center',
        alignItems: 'center',
        zIndex: 10,
        borderRadius: 8,
        padding: 16
      }}>
        {/* Increase the size of LottieView */}
        <LottieView
          source={loaderSource}
          autoPlay
          loop
          style={{ width: 300, height: 300 }}
        />
        <Text style={{ 
          fontFamily: 'Finlandica', 
          fontSize: 18, 
          fontWeight: "bold", 
          color: textColor,
          textAlign: 'center',
          marginTop: 10
        }}>
          {status.statusMessage || (status.action === 'connect' ? 'Connecting...' : 'Disconnecting...')}
        </Text>
        {status.instructions && (
          <Text style={{ 
            fontFamily: 'Finlandica', 
            fontSize: 16, 
            fontWeight: "bold",
            color: textColor, // Use same color as status text, not green
            textAlign: 'center',
            marginTop: 12
          }}>
            {status.instructions}
          </Text>
        )}
        {status.error && (
          <Text style={{ 
            fontFamily: 'Finlandica', 
            fontSize: 14, 
            color: textColor, // Use same color as status text, not red
            textAlign: 'center',
            marginTop: 8
          }}>
            {status.error}
          </Text>
        )}
      </View>
    );
  };

  // Add local state for slider values and mute status
  const [sliderValues, setSliderValues] = useState<{
    [mac: string]: {
      volume: number;
      latency: number;
      balance: number;
      isMuted: boolean;
    }
  }>({});

  // Update slider values when settings change
  useEffect(() => {
    const newSliderValues: {
      [mac: string]: {
        volume: number;
        latency: number;
        balance: number;
        isMuted: boolean;
      }
    } = {};
    
    Object.keys(settings).forEach(mac => {
      newSliderValues[mac] = {
        volume: settings[mac]?.volume ?? 50,
        latency: settings[mac]?.latency ?? 100,
        balance: 0.5, // Default balance value
        isMuted: false // Default mute state
      };
    });
    
    setSliderValues(newSliderValues);
  }, [settings]);

  // Load speakers either from the database (if configID exists) or from URL.
  useEffect(() => {
    if (configIDParam) {
      const configIdNum = Number(configIDParam);
  
      // Use the new getSpeakersFull to load *all* speaker data, including is_connected.
      const fullRows = getSpeakersFull(configIdNum);
  
      // Build `connectedSpeakers` and `settings` from these rows.
      const mapping: { [mac: string]: string } = {};
      const loadedSettings: {
        [mac: string]: { volume: number; latency: number; isConnected: boolean };
      } = {};
  
      fullRows.forEach(row => {
        mapping[row.mac] = row.name;
        loadedSettings[row.mac] = {
          volume: row.volume,
          latency: row.latency,
          isConnected: row.is_connected === 1, // Convert DB 0/1 to boolean
        };
      });
  
      setConnectedSpeakers(mapping);
      setSettings(loadedSettings);
  
      // For the overall config status:
      try {
        const status = getConfigurationStatus(configIdNum);
        setIsConnected(status === 1);
      } catch (err) {
        console.error("Error fetching connection status:", err);
      }
    } else {
      // If configIDParam does not exist, we handle a new config or URL with speakers.
      try {
        const spk = speakersStr ? JSON.parse(speakersStr) : {};
        setConnectedSpeakers(spk);
  
        const defaultSettings: {
          [mac: string]: { volume: number; latency: number; isConnected: boolean }
        } = {};
  
        Object.keys(spk).forEach(mac => {
          defaultSettings[mac] = {
            volume: 50,
            latency: 100,
            isConnected: false,
          };
        });
        setSettings(defaultSettings);
      } catch (e) {
        console.error("Error parsing speakers param:", e);
        setConnectedSpeakers({});
      }
    }
  }, [configIDParam, speakersStr, dbUpdateTrigger]);
  
  // Replace UI speaker state from backend when we have full state (e.g. on app open)
  useEffect(() => {
    if (!piStatus || !Array.isArray(piStatus.connected) || Object.keys(connectedSpeakers).length === 0) return;
    const connectedMacs = (piStatus.connected as string[]).map((m: string) => m.toUpperCase());
    setSettings(prev => {
      let changed = false;
      const next = { ...prev };
      Object.keys(connectedSpeakers).forEach(mac => {
        if (!next[mac]) return;
        const isConnected = connectedMacs.includes(mac.toUpperCase());
        if (next[mac].isConnected !== isConnected) {
          next[mac] = { ...next[mac], isConnected };
          changed = true;
        }
      });
      return changed ? next : prev;
    });
    if (configIDParam) {
      const configIdNum = Number(configIDParam);
      Object.keys(connectedSpeakers).forEach(mac => {
        updateSpeakerConnectionStatus(configIdNum, mac, connectedMacs.includes(mac.toUpperCase()));
      });
    }
  }, [piStatus?.connected, configIDParam]); // intentional: run when backend connected list arrives

  // Listen for piStatus updates to track connection changes
  useEffect(() => {
    if (!piStatus || piStatus.connected === undefined) return;
    
    // Get the list of connected MACs from piStatus
    const connectedMacs = (piStatus.connected || []).map((mac: string) => mac.toUpperCase());
    console.log('[Speaker] Connected MACs from piStatus:', connectedMacs);
    
    // Check all speakers in our configuration
    Object.keys(connectedSpeakers).forEach(mac => {
      const upperMac = mac.toUpperCase();
      const isConnected = connectedMacs.includes(upperMac);
      const currentStatus = loadingSpeakers[mac]?.action;
      
      // Speaker connected
      if (isConnected) {
        // Only update if:
        // 1. The speaker is in 'connect' action
        // 2. We haven't processed this connection yet
        // 3. It's not already showing success
        if (
          currentStatus === 'connect' && 
          !processedConnections.has(mac) &&
          loadingSpeakers[mac]?.statusMessage !== "Connection successful!"
        ) {
          console.log(`[Speaker] Confirming connection success for ${mac}`);
          
          // Mark as processed
          setProcessedConnections(prev => {
            const newSet = new Set(prev);
            newSet.add(mac);
            return newSet;
          });
          
          // Show success status
          setLoadingSpeakers(prev => ({
            ...prev,
            [mac]: {
              action: 'connect',
              statusMessage: "Connection successful!",
              success: true
            }
          }));
          
          // Update local state
          setSettings(prev => {
            const updatedSettings = { ...prev };
            if (updatedSettings[mac]) {
              updatedSettings[mac].isConnected = true;
            }
            return updatedSettings;
          });
          
          // Update database
          if (configIDParam) {
            updateSpeakerConnectionStatus(Number(configIDParam), mac, true);
          }
          
          // Clear after 3 seconds
          setTimeout(() => {
            console.log(`[Speaker] Clearing success overlay for ${mac}`);
            setLoadingSpeakers(prev => ({ ...prev, [mac]: null }));
            
            // Also clear from processed list after a delay to allow reconnections
            setTimeout(() => {
              setProcessedConnections(prev => {
                const newSet = new Set(prev);
                newSet.delete(mac);
                return newSet;
              });
            }, 5000);
          }, 3000);
        }
      }
      
      // Speaker disconnected
      else if (!isConnected && currentStatus === 'disconnect') {
        // Prevent processing the same disconnection multiple times
        if (!processedDisconnections.has(mac)) {
          console.log(`[Speaker] Confirming disconnection success for ${mac}`);
          
          // Mark this disconnection as processed
          setProcessedDisconnections(prev => {
            const newSet = new Set(prev);
            newSet.add(mac);
            return newSet;
          });
          
          // Show success status
          setLoadingSpeakers(prev => ({
            ...prev,
            [mac]: {
              action: 'disconnect',
              statusMessage: "Speaker disconnected successfully",
              success: true
            }
          }));
          
          // Update local state
          setSettings(prev => {
            const updatedSettings = { ...prev };
            if (updatedSettings[mac]) {
              updatedSettings[mac].isConnected = false;
            }
            return updatedSettings;
          });
          
          // Update database
          if (configIDParam) {
            updateSpeakerConnectionStatus(Number(configIDParam), mac, false);
          }
          
          // Clear after 2 seconds
          setTimeout(() => {
            console.log(`[Speaker] Clearing disconnect overlay for ${mac}`);
            setLoadingSpeakers(prev => ({ ...prev, [mac]: null }));
            
            // Also clear from processed list after a delay to allow reconnections
            setTimeout(() => {
              setProcessedDisconnections(prev => {
                const newSet = new Set(prev);
                newSet.delete(mac);
                return newSet;
              });
            }, 5000);
          }, 2000);
        }
      }
    });
  }, [piStatus, connectedSpeakers, configIDParam, loadingSpeakers, processedConnections, processedDisconnections]);

  // Listen for BLE connection status updates
  useEffect(() => {
    if (!bleConnectionStatus?.mac) return;
    const mac = bleConnectionStatus.mac.toString();

    // Failure phases: stop spinner and show error, then clear
    const failurePhases = ['discovery_timeout', 'pairing_failed', 'connect_failed', 'connect_profile_failed'];
    const isFailure = failurePhases.some(p => bleConnectionStatus.status?.toLowerCase().includes(p)) || !!bleConnectionStatus.error;
    if (isFailure) {
      setLoadingSpeakers(prev => ({
        ...prev,
        [mac]: {
          action: null,
          statusMessage: bleConnectionStatus.status ?? 'Connection failed',
          error: bleConnectionStatus.error ?? bleConnectionStatus.status
        }
      }));
      // Clear overlay after 5s so user can retry
      const t = setTimeout(() => {
        setLoadingSpeakers(prev => ({ ...prev, [mac]: null }));
      }, 5000);
      return () => clearTimeout(t);
    }

    // In-progress: show status (spinner stays until success or failure)
    const currentStatus = loadingSpeakers[mac]?.action;
    if (currentStatus === 'connect') {
      setLoadingSpeakers(prev => ({
        ...prev,
        [mac]: {
          action: 'connect',
          statusMessage: bleConnectionStatus.status,
          progress: bleConnectionStatus.progress,
          error: bleConnectionStatus.error
        }
      }));
    }
  }, [bleConnectionStatus]);

  const handleVolumeChangeWrapper = async (mac: string, newVolume: number, isSlidingComplete: boolean) => {
    await handleVolumeChange(
      mac,
      newVolume,
      settings,
      setSettings,
      configIDParam,
      updateSpeakerSettings,
      connectedDevice,
      isSlidingComplete
    );
  };

  const handleLatencyChangeWrapper = async (mac: string, newLatency: number, isSlidingComplete: boolean) => {
    await handleLatencyChange(
      mac,
      newLatency,
      settings,
      setSettings,
      configIDParam,
      updateSpeakerSettings,
      isSlidingComplete,
      connectedDevice
    );
    if (isSlidingComplete) {
      await playSound();
    }
  };

  const handleSoundFieldChange = async (mac: string, newBalance: number, isSlidingComplete: boolean) => {
    // Update local state immediately
    setSliderValues(prev => ({
      ...prev,
      [mac]: { ...prev[mac], balance: newBalance }
    }));

    // If still sliding, don't do server/database updates
    if (!isSlidingComplete) {
      return;
    }
    
    try {
      if (!connectedDevice) {
        console.error('No BLE device connected for sound field change');
        Alert.alert("Error", "No BLE device connected");
        return;
      }

      // Use the same setVolume function with the current volume and new balance
      await setVolume(
        connectedDevice,
        mac,
        settings[mac]?.volume || 50,
        newBalance
      );

      // Update database if we have a config ID
      if (configIDParam) {
        updateSpeakerSettings(
          Number(configIDParam),
          mac,
          settings[mac]?.volume || 50,
          settings[mac]?.latency || 100,
          newBalance
        );
      }
    } catch (error) {
      console.error("Error updating sound field:", error);
      Alert.alert("Error", "Failed to update sound field settings.");
    }
  };

  const handleConnectOne = async (mac: string) => {
    // Reset the processed state for this MAC when starting a new connect
    setProcessedConnections(prev => {
      const newSet = new Set(prev);
      newSet.delete(mac);
      return newSet;
    });
    
    console.log('handleConnectOne triggered for mac:', mac);
    
    if (!connectedDevice) {
      console.log('No BLE device connected');
      Alert.alert("Error", "No Bluetooth device connected");
      return;
    }

    console.log('BLE device found:', connectedDevice.id);
    
    // Show loading indicator overlay on the speaker card
    setLoadingSpeakers(prev => ({ ...prev, [mac]: { 
      action: 'connect',
      statusMessage: "Starting connection process..."
    }}));
    
    try {
      console.log('Attempting bleConnectOne with settings:', {
        mac,
        name: connectedSpeakers[mac],
        settings: {
          volume: settings[mac]?.volume || 50,
          latency: settings[mac]?.latency || 100,
          balance: sliderValues[mac]?.balance || 0.5
        }
      });

      // Get all speaker MACs in the configuration from the database
      const allSpeakers = configIDParam ? getSpeakersFull(Number(configIDParam)) : [];
      const allowedMacs = allSpeakers.map(speaker => speaker.mac);

      // Pass the connected device to bleConnectOne
      await bleConnectOne(
        connectedDevice,
        mac,
        connectedSpeakers[mac],
        {
          volume: settings[mac]?.volume || 50,
          latency: settings[mac]?.latency || 100,
          balance: sliderValues[mac]?.balance || 0.5
        },
        allowedMacs
      );
      
      // Set a fallback timeout in case no notification is received
      setTimeout(() => {
        setLoadingSpeakers(prev => {
          // Only clear if still in the initial state
          if (prev[mac]?.action === 'connect' && prev[mac]?.statusMessage === "Starting connection process...") {
            return { ...prev, [mac]: null };
          }
          return prev;
        });
      }, 120000); // 2 minute timeout
    } catch (error) {
      console.error("Error connecting speaker:", error);
      
      // Show error in the overlay
      setLoadingSpeakers(prev => ({ ...prev, [mac]: { 
        action: 'connect',
        statusMessage: "Connection failed",
        error: "Failed to connect speaker. Please try again."
      }}));
      
      // Clear error after 5 seconds
      setTimeout(() => {
        setLoadingSpeakers(prev => ({ ...prev, [mac]: null }));
      }, 5000);
    }
  };

  const handleDisconnectOne = async (mac: string) => {
    // Reset the processed state for this MAC when starting a new disconnect
    setProcessedDisconnections(prev => {
      const newSet = new Set(prev);
      newSet.delete(mac);
      return newSet;
    });
    
    // Show loading indicator overlay on the speaker card
    setLoadingSpeakers(prev => ({ ...prev, [mac]: { 
      action: 'disconnect',
      statusMessage: ""
    }}));
    
    if (!connectedDevice) {
      console.log('No BLE device connected');
      Alert.alert("Error", "No Bluetooth device connected");
      return;
    }

    try {
      await bleDisconnectOne(connectedDevice, mac);
      
      // Set a fallback timeout in case no notification is received
      setTimeout(() => {
        setLoadingSpeakers(prev => {
          // Only update if still in the initial disconnecting state
          if (prev[mac]?.action === 'disconnect' && prev[mac]?.statusMessage === "Disconnecting Speaker...") {
            console.log(`[Speaker] Disconnect fallback timeout triggered for ${mac}`);
            return { ...prev, [mac]: { 
              action: 'disconnect',
              statusMessage: "Disconnect may have failed. Please try again.",
              error: "No confirmation received from device."
            }};
          }
          return prev;
        });
        
        // Clear the error after 5 seconds
        setTimeout(() => {
          setLoadingSpeakers(prev => {
            if (prev[mac]?.error === "No confirmation received from device.") {
              return { ...prev, [mac]: null };
            }
            return prev;
          });
        }, 5000);
      }, 120000); // 2 minute timeout
    } catch (error) {
      console.error("Error disconnecting speaker:", error);
      
      // Show error in the overlay
      setLoadingSpeakers(prev => ({ ...prev, [mac]: { 
        action: 'disconnect',
        statusMessage: "Disconnection failed",
        error: "Failed to disconnect speaker. Please try again."
      }}));
      
      // Clear error after 5 seconds
      setTimeout(() => {
        setLoadingSpeakers(prev => ({ ...prev, [mac]: null }));
      }, 5000);
    }
  };

  const handleUltrasonicSync = async () => {
    Alert.alert(
      'Auto-sync unavailable',
      'Ultrasonic auto-alignment is disabled on the neutral foundation branch and will return on Epic 3.'
    );
  };

  const handleAlignAllStartupTune = () => {
    if (!connectedDevice) {
      Alert.alert('Error', 'No Bluetooth device connected');
      return;
    }
    Alert.alert(
      'Startup tune alignment',
      'Pause music on your phone first. The Pi will play a short chirp through connected speakers.',
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Continue',
          onPress: () => {
            void (async () => {
              try {
                await calibrateAllSpeakers(connectedDevice, {
                  calibration_mode: 'startup_tune',
                });
              } catch (error) {
                console.error(error);
                Alert.alert('Error', 'Failed to send align-all command.');
              }
            })();
          },
        },
      ],
    );
  };

  const handleAlignAllMusic = () => {
    if (!connectedDevice) {
      Alert.alert('Error', 'No Bluetooth device connected');
      return;
    }
    Alert.alert(
      'Align using current audio',
      'Uses whatever is already playing through the Pi. Works best when music is dense (not silence).',
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Continue',
          onPress: () => {
            void (async () => {
              try {
                await calibrateAllSpeakers(connectedDevice, {
                  calibration_mode: 'music',
                });
              } catch (error) {
                console.error(error);
                Alert.alert('Error', 'Failed to send align-all command.');
              }
            })();
          },
        },
      ],
    );
  };

  // Single-speaker calibration is intentionally not surfaced as a UI
  // button: alignment is only meaningful relative to peers, so a one-
  // speaker action would never produce user value. The CALIBRATE_SPEAKER
  // BLE opcode and CLI remain for diagnostics.

  const handleMuteToggle = async (mac: string) => {
    const isCurrentlyMuted = sliderValues[mac]?.isMuted || false;
    const newMuted = !isCurrentlyMuted;

    if (!connectedDevice) {
      console.error('No BLE device connected for mute toggle');
      Alert.alert("Error", "No BLE device connected");
      return;
    }

    // Optimistic update: apply mute state in UI immediately
    setSliderValues(prev => ({
      ...prev,
      [mac]: { ...prev[mac], isMuted: newMuted }
    }));

    try {
      await setMute(connectedDevice, mac, newMuted);

      if (configIDParam) {
        updateSpeakerSettings(
          Number(configIDParam),
          mac,
          settings[mac]?.volume || 50,
          settings[mac]?.latency || 100,
          sliderValues[mac]?.balance || 0.5,
          newMuted
        );
      }
    } catch (error) {
      console.error("Error toggling mute:", error);
      // Revert optimistic update on failure
      setSliderValues(prev => ({
        ...prev,
        [mac]: { ...prev[mac], isMuted: isCurrentlyMuted }
      }));
      Alert.alert("Error", "Failed to toggle mute.");
    }
  };

  const themeName = useThemeName();
      const theme = useTheme();
    
    
      const imageSource = themeName === 'dark'
        ? require('../assets/images/welcomeGraphicDark.png')
        : require('../assets/images/welcomeGraphicLight.png')
     
        const bg = themeName === 'dark' ? '#250047' : '#F2E8FF'   //background
        const pc = themeName === 'dark' ? '#E8004D' : '#3E0094'   //primary (pink/purple)
        const tc = themeName === 'dark' ? '#F2E8FF' : '#26004E'   // text color
        const stc = themeName === 'dark' ? '#9D9D9D' : '#9D9D9D'    // subtext color
        const green = themeName === 'dark' ? '#00FF6A' : '#34A853'    // green is *slightly* different on light/dark 
        const red = themeName === 'dark' ? '' : '#E8004D'  // red is actually black on dark mode due to similarity of pc

        //if android
            let buffer = 20
            //else, 
            if (Platform.OS === 'ios') {
              buffer = 0
            }

      const { width: screenWidth } = Dimensions.get('window');

    // Estimate the font size based on screen width and expected text length
    // You can tweak the divisor (e.g., 0.05 * screenWidth) to find the best fit
    const estimatedFontSize = Math.min(40, screenWidth / (configNameParam.length + 12));

    const anyPiSpeakerConnected = Object.keys(settings).some(
      (mac) => settings[mac]?.isConnected
    );

      return (
        <YStack flex={1} backgroundColor={bg}>
          
          {/* Top Bar with Back Button -----------------------------------------------------------------*/}
          <TopBar/>
          
          {/* Header -----------------------------------------------------------------------------------*/}
          <Header title={configNameParam}/>
          
          <ScrollView contentContainerStyle={{ paddingBottom: 15 }}>
            {connectedDevice && anyPiSpeakerConnected && Object.keys(connectedSpeakers).length > 0 && (() => {
              const last = calibrationEvents.length > 0
                ? (calibrationEvents[calibrationEvents.length - 1] as Record<string, unknown>)
                : null;
              const lastPhase = last ? String(last.phase ?? '') : '';
              const seqInFlight =
                !!lastPhase &&
                lastPhase !== 'sequence_complete' &&
                lastPhase !== 'sequence_failed' &&
                (last?.sequence_total !== undefined || lastPhase === 'sequence_started');
              const seqIdx = (last?.sequence_index as number | undefined) ?? 0;
              const seqTotal = (last?.sequence_total as number | undefined) ?? 0;
              const seqMac = String(last?.mac ?? '');
              const seqName = connectedSpeakers[seqMac] ?? seqMac;
              return (
                <YStack alignSelf="center" marginTop={12} marginBottom={8} width="90%">
                  <Button
                    backgroundColor={pc as any}
                    color="white"
                    disabled={seqInFlight}
                    onPress={handleAlignAllStartupTune}
                  >
                    <Text fontFamily="Finlandica" color="white">
                      {seqInFlight && seqTotal > 0
                        ? `Aligning ${seqIdx}/${seqTotal} (${seqName})`
                        : 'Align all speakers (startup tune)'}
                    </Text>
                  </Button>
                  <Button
                    marginTop={10}
                    backgroundColor={themeName === 'dark' ? '#3D2A55' : '#E8DCFA'}
                    color={tc as any}
                    disabled={seqInFlight}
                    onPress={handleAlignAllMusic}
                  >
                    <Text fontFamily="Finlandica" color={tc}>
                      Align all (use playing music)
                    </Text>
                  </Button>
                  <Body center style={{ marginTop: 8, fontSize: 12, color: stc }}>
                    {seqInFlight
                      ? `${String(lastPhase).replace(/_/g, ' ')}…`
                      : 'Mic alignment runs on the Pi. Pause phone playback before startup tune so the reference signal stays clean.'}
                  </Body>
                </YStack>
              );
            })()}
            {/* Auto-sync speakers (ultrasonic) – runs one sync cycle on the Pi */}
            {Object.keys(connectedSpeakers).length >= 2 && (
              <YStack alignSelf="center" marginTop={12} marginBottom={8} width="90%">
                <Button
                  backgroundColor={pc as any}
                  color="white"
                  disabled={!connectedDevice}
                  onPress={handleUltrasonicSync}
                >
                  <Text fontFamily="Finlandica" color="white">
                    Auto-sync speakers
                  </Text>
                </Button>
                <Body center style={{ marginTop: 6, fontSize: 12, color: stc }}>
                  Reserved for Epic 3. The neutral foundation keeps this UI visible but disables runtime auto-sync.
                </Body>
              </YStack>
            )}

            {Object.keys(connectedSpeakers).length === 0 ? (
              <Text style={{ fontFamily: 'Finlandica' }}>No connected speakers found.</Text>
            ) : (
              Object.keys(connectedSpeakers).map((mac, index) => (
                <SafeAreaView key={mac} style={{ width:"90%",
                                                alignSelf:"center", 
                                                marginTop: index === 0 ? 15 : 0, // 👈 only the first item gets top margin
                                                marginBottom: 15, 
                                                paddingLeft: 20, 
                                                paddingRight: 20, 
                                                paddingBottom: 5 + buffer, 
                                                paddingTop: 5 + buffer,
                                                backgroundColor: bg,
                                                borderWidth: 1, 
                                                borderColor: stc,
                                                borderRadius: 8, 
                                                shadowColor: tc,
                                                shadowOffset: { width: 0, height: 0 },
                  shadowOpacity: 0.5,
                  shadowRadius: 8,
                  elevation: themeName === 'dark' ? 15 : 10,
                }}>
                  
                  <View style={{
                    position: 'absolute',
                    top: 20,
                    left: 15,
                    width: 16,
                    height: 16,
                    borderRadius: 8,
                    backgroundColor: settings[mac]?.isConnected ? green : '#FF0055',
                    shadowColor: themeName === 'dark' ? '#000000' : tc,
                    shadowOffset: { width: 0, height: 0 },
                    shadowOpacity: themeName === 'dark' ? 0.9 : 0.6,
                    shadowRadius: themeName === 'dark' ? 6 : 4,
                    elevation: themeName === 'dark' ? 8 : 5,
                    borderWidth: themeName === 'dark' ? 1 : 0,
                    borderColor: themeName === 'dark' ? 'rgba(255,255,255,0.1)' : 'transparent',
                    zIndex: 2
                  }} />
                  <Text style={{ 
                    fontFamily: 'Finlandica', 
                    fontSize: 24, 
                    fontWeight: "bold", 
                    color: tc, 
                    alignSelf: 'center',
                    marginTop: 0
                  }}>
                    {connectedSpeakers[mac]}
                  </Text>
                  <Body center={false} bold={true} style={{fontSize: 18, letterSpacing: 1}}>
                    Volume: {settings[mac]?.volume || 50}%
                  </Body>
                  <Slider
                    style={styles.slider}
                    minimumValue={0}
                    maximumValue={100}
                    step={1}
                    value={settings[mac]?.volume || 50}
                    onValueChange={(value: number) => handleVolumeChangeWrapper(mac, value, false)}
                    onSlidingComplete={(value: number) => handleVolumeChangeWrapper(mac, value, true)}
                    minimumTrackTintColor={pc}
                    maximumTrackTintColor="#000000"
                    thumbTintColor="white" 
                  />

                  {/* Connection status overlay - appears on top of the card when connecting/disconnecting */}
                  {loadingSpeakers[mac] && <SpeakerCardOverlay mac={mac} status={loadingSpeakers[mac]} />}
                  <Body center={false} bold={true} style={{fontSize: 18, letterSpacing: 1}}>
                    Latency: {settings[mac]?.latency ?? 100} ms
                  </Body>
                  <Slider
                    style={styles.slider}
                    minimumValue={0}
                    // Unified slider scale across BT + Wi-Fi cards so
                    // relative positions stay meaningful when a Wi-Fi
                    // anchor (300-1500+ ms) is in the mix. BT-only
                    // configs use the tighter 500 ms scale.
                    maximumValue={unifiedSliderMax}
                    step={10}
                    value={settings[mac]?.latency ?? 100}
                    onValueChange={(value: number) => handleLatencyChangeWrapper(mac, value, false)}
                    onSlidingComplete={(value: number) => handleLatencyChangeWrapper(mac, value, true)}
                    minimumTrackTintColor={pc}
                    maximumTrackTintColor="#000000"
                    thumbTintColor="white" 
                  />
                  {settings[mac]?.isConnected && (() => {
                    // Per-speaker calibration result strip. The
                    // "Align all speakers (startup tune)" button at the
                    // top of the screen is the user-facing entry point;
                    // a single-speaker button here added no value
                    // because alignment is only meaningful relative to
                    // peers. We still render the per-speaker outcome
                    // because that is what the user actually needs to
                    // see after pressing Align all.
                    const macUp = mac.toUpperCase();
                    let lastForMac: Record<string, unknown> | null = null;
                    for (let i = calibrationEvents.length - 1; i >= 0; i -= 1) {
                      const ev = calibrationEvents[i] as Record<string, unknown>;
                      if (typeof ev.mac === 'string' && (ev.mac as string).toUpperCase() === macUp) {
                        lastForMac = ev;
                        break;
                      }
                    }
                    const phase = lastForMac ? String(lastForMac.phase ?? '') : '';
                    if (!phase) return null;
                    const inFlight =
                      phase !== 'applied' &&
                      phase !== 'failed' &&
                      phase !== 'sequence_complete' &&
                      phase !== 'sequence_failed';
                    if (inFlight) {
                      return (
                        <Body center style={{ marginTop: 8, fontSize: 12, color: stc }}>
                          Calibrating: {phase.replace(/_/g, ' ')}…
                        </Body>
                      );
                    }
                    if (phase === 'applied' && lastForMac) {
                      const m = lastForMac.measurement as Record<string, number> | undefined;
                      return (
                        <Body center style={{ marginTop: 8, fontSize: 12, color: stc }}>
                          Aligned: {Math.round(m?.lag_ms ?? 0)} ms lag → {Math.round((lastForMac.new_user_delay_ms as number) ?? 0)} ms delay (conf {(m?.confidence_secondary ?? 0).toFixed(1)}x)
                        </Body>
                      );
                    }
                    if (phase === 'failed' && lastForMac) {
                      return (
                        <Body center style={{ marginTop: 8, fontSize: 12, color: '#FF0055' }}>
                          {String(lastForMac.reason ?? lastForMac.detail ?? 'Calibration failed')}
                        </Body>
                      );
                    }
                    return null;
                  })()}
                  <View style={styles.soundFieldContainer}>
                    {/* Left Side */}
                    <View style={{ flexDirection: 'row', alignItems: 'center' }}>
                      <View style={{
                        width: 24,
                        height: 24,
                        borderRadius: 12,
                        borderWidth: 2,
                        borderColor: tc,
                        justifyContent: 'center',
                        alignItems: 'center',
                        marginRight: 6
                      }}>
                        <Text style={{
                          fontFamily: 'Inter',
                          fontSize: 14,
                          fontWeight: 'bold',
                          color: tc,
                        }}>
                          L
                        </Text>
                      </View>
                      <View style={[styles.speakerIconContainer]}>
                        {(() => {
                          const balance = sliderValues[mac]?.balance ?? 0.5;
                          // Left side is active when balance is < 0.5
                          if (balance < 0.5) {
                            return <Volume2 size={20} color={tc} />;
                          } else {
                            const rightValue = (balance - 0.5) * 2;
                            if (rightValue > 0.8) return <VolumeX size={20} color={tc} />;
                            if (rightValue > 0.3) return <Volume1 size={20} color={tc} />;
                            return <Volume2 size={20} color={tc} />;
                          }
                        })()}
                        <View style={styles.soundWaves}>
                          {[3, 2, 1].map((i) => (
                            <View 
                              key={i} 
                              style={[
                                styles.soundWaveBar,
                                { 
                                  opacity: (sliderValues[mac]?.balance ?? 0.5) <= 0.5 ? 
                                    (0.5 - (sliderValues[mac]?.balance ?? 0.5)) * 2 * i : 0,
                                  backgroundColor: tc
                                }
                              ]} 
                            />
                          ))}
                        </View>
                      </View>
                    </View>

                    {/* Middle */}
                  <Body center={false} bold={true} style={{fontSize: 18, letterSpacing: 1}}>
                    Balance
                  </Body>

                    {/* Right */}
                    <View style={{ flexDirection: 'row', alignItems: 'center' }}>
                      <View style={[styles.speakerIconContainer]}>
                        <View style={styles.soundWaves}>
                          {[1, 2, 3].map((i) => (
                            <View 
                              key={i} 
                              style={[
                                styles.soundWaveBar,
                                { 
                                  opacity: (sliderValues[mac]?.balance ?? 0.5) >= 0.5 ? 
                                    ((sliderValues[mac]?.balance ?? 0.5) - 0.5) * 2 * i : 0,
                                  backgroundColor: tc
                                }
                              ]} 
                            />
                          ))}
                        </View>
                        <View style={{ transform: [{ scaleX: -1 }] }}>
                          {(() => {
                            const balance = sliderValues[mac]?.balance ?? 0.5;
                            // Right side is active when balance is > 0.5
                            if (balance > 0.5) {
                              return <Volume2 size={20} color={tc} />;
                            } else {
                              const leftValue = (0.5 - balance) * 2;
                              if (leftValue > 0.8) return <VolumeX size={20} color={tc} />;
                              if (leftValue > 0.3) return <Volume1 size={20} color={tc} />;
                              return <Volume2 size={20} color={tc} />;
                            }
                          })()}
                        </View>
                      </View>
                      <View style={{
                        width: 24,
                        height: 24,
                        borderRadius: 12,
                        borderWidth: 2,
                        borderColor: tc,
                        justifyContent: 'center',
                        alignItems: 'center',
                        marginLeft: 6,
                      }}>
                        <Text style={{
                          fontFamily: 'Inter',
                          fontSize: 14,
                          fontWeight: 'bold',
                          color: tc,
                        }}>
                          R
                        </Text>
                      </View>
                    </View>
                  </View>
                  <Slider
                    style={styles.slider}
                    minimumValue={0}
                    maximumValue={1}
                    step={0.01}
                    value={sliderValues[mac]?.balance ?? 0.5}
                    onValueChange={(value: number) => {
                      // Add "magnetic" effect to center with visual snap
                      const magneticRange = 0.05; // 5% range around center where it will snap
                      const adjustedValue = Math.abs(value - 0.5) < magneticRange ? 0.5 : value;
                      
                      setSliderValues(prev => ({
                        ...prev,
                        [mac]: { ...prev[mac], balance: adjustedValue }
                      }));
                      handleSoundFieldChange(mac, adjustedValue, false);
                    }}
                    onSlidingComplete={(value: number) => {
                      // Also apply magnetic effect on slide complete
                      const magneticRange = 0.05;
                      const adjustedValue = Math.abs(value - 0.5) < magneticRange ? 0.5 : value;
                      
                      setSliderValues(prev => ({
                        ...prev,
                        [mac]: { ...prev[mac], balance: adjustedValue }
                      }));
                      handleSoundFieldChange(mac, adjustedValue, true);
                    }}
                    minimumTrackTintColor="#000000"
                    maximumTrackTintColor="#000000"
                    thumbTintColor="white"
                  />
                  <View style={{ flexDirection: 'row', justifyContent: 'space-between', marginTop: 30, marginBottom: 10, paddingHorizontal: 10 }}>
                    <TouchableOpacity 
                      onPress={() => handleConnectOne(mac)}
                      disabled={!!loadingSpeakers[mac]?.action}
                      style={{ flex: 1, alignItems: 'center' }}
                    >
                      <Text style={{ 
                        fontFamily: 'Finlandica', 
                        fontSize: 18, 
                        fontWeight: "bold", 
                        color: !!loadingSpeakers[mac]?.action ? stc : themeName === 'dark' ? '#FFFFFF' : '#3E0094'
                      }}>
                        {loadingSpeakers[mac]?.action === 'connect' 
                          ? 'Connecting...' 
                          : loadingSpeakers[mac]?.statusMessage && loadingSpeakers[mac]?.action === null && loadingSpeakers[mac]?.success
                          ? 'Connected'
                          : 'Connect'}
                      </Text>
                      {loadingSpeakers[mac]?.statusMessage && loadingSpeakers[mac]?.action === 'connect' && (
                        <Text style={{ 
                          fontFamily: 'Finlandica', 
                          fontSize: 10, 
                          color: themeName === 'dark' ? '#CCCCCC' : '#666666',
                          textAlign: 'center',
                          marginTop: 4
                        }}>
                          {loadingSpeakers[mac]?.statusMessage}
                        </Text>
                      )}
                      {loadingSpeakers[mac]?.error && (
                        <Text style={{ 
                          fontFamily: 'Finlandica', 
                          fontSize: 10, 
                          color: '#FF0055',
                          textAlign: 'center',
                          marginTop: 4
                        }}>
                          {loadingSpeakers[mac]?.error}
                        </Text>
                      )}
                    </TouchableOpacity>
                    <TouchableOpacity onPress={() => handleMuteToggle(mac)} style={{ flex: 0.5, alignItems: 'center' }}>
                      {sliderValues[mac]?.isMuted ? (
                        <VolumeX size={24} color="#FF0055" />
                      ) : (
                        <Volume2 size={24} color={themeName === 'dark' ? '#FFFFFF' : pc} />
                      )}
                    </TouchableOpacity>
                    <TouchableOpacity 
                      onPress={() => handleDisconnectOne(mac)}
                      disabled={!!loadingSpeakers[mac]?.action}
                      style={{ flex: 1, alignItems: 'center' }}
                    >
                      <Text style={{ 
                        fontFamily: 'Finlandica', 
                        fontSize: 18, 
                        fontWeight: "bold", 
                        color: !!loadingSpeakers[mac]?.action ? stc : '#FF0055'
                      }}>
                        {loadingSpeakers[mac]?.action === 'disconnect' 
                          ? 'Disconnecting' 
                          : loadingSpeakers[mac]?.statusMessage && loadingSpeakers[mac]?.action === null && !loadingSpeakers[mac]?.success
                          ? 'Disconnected'
                          : 'Disconnect'}
                      </Text>
                      {loadingSpeakers[mac]?.statusMessage && loadingSpeakers[mac]?.action === 'disconnect' && (
                        <Text style={{ 
                          fontFamily: 'Finlandica', 
                          fontSize: 10, 
                          color: themeName === 'dark' ? '#CCCCCC' : '#666666',
                          textAlign: 'center',
                          marginTop: 4
                        }}>
                          {loadingSpeakers[mac]?.statusMessage}
                        </Text>
                      )}
                      {loadingSpeakers[mac]?.error && (
                        <Text style={{ 
                          fontFamily: 'Finlandica', 
                          fontSize: 10, 
                          color: '#FF0055',
                          textAlign: 'center',
                          marginTop: 4
                        }}>
                          {loadingSpeakers[mac]?.error}
                        </Text>
                      )}
                    </TouchableOpacity>
                  </View>
                </SafeAreaView>
              ))
            )}
            
           
          </ScrollView>
        </YStack>
      );
    }
    
    const styles = StyleSheet.create({
      container: { flex: 1, padding: 20, backgroundColor: '#fff' },
      header: { fontSize: 24, fontWeight: 'bold', marginBottom: 20, textAlign: 'center' },
      speakerContainer: {},
      speakerName: { fontSize: 18, marginBottom: 10 },
      label: { fontSize: 15, marginTop: 10, fontWeight: "bold"},
      slider: { width: '100%', height: 40, marginBottom: -5},
      instructions: { fontSize: 14, marginTop: 10, textAlign: 'center' },
      buttonContainer: { alignItems: "center", flexDirection: 'row', justifyContent: 'space-around', marginTop: 75 },
      saveButton: { backgroundColor: '#3E0094', padding: 15, borderRadius: 8 },
      disconnectButton: { backgroundColor: "#FFFFFF", padding: 15, borderRadius: 8 },
      deleteButton: { backgroundColor: '#FF0055', padding: 15, borderRadius: 8 },
      buttonText: { color: '#fff', fontSize: 18, fontFamily: "Finlandica", alignSelf: 'center', },
      homeButton: {
        position: 'absolute',
        bottom: 20,
        left: 20,
        padding: 15,
        borderRadius: 15,
        backgroundColor: '#3E0094',
        justifyContent: 'center',
        alignItems: 'center',
      },
      homeButtonText: { 
        color: '#F2E8FF', 
        fontSize: 16,
        fontFamily: "Finlandica"
      },
      disabledButton: {
        opacity: 0.7,
      },
      statusDot: {
        width: 20,
        height: 20,
        borderRadius: 10
      },
      soundFieldContainer: {
        flexDirection: 'row',
        justifyContent: 'space-between',
        marginTop: 10,
        marginBottom: 5,
        alignItems: 'center',
      },
      speakerIconContainer: {
        flexDirection: 'row',
        alignItems: 'center',
      },
      soundWaves: {
        flexDirection: 'row',
        marginLeft: 4,
        marginRight: 2
      },
      soundWaveBar: {
        width: 3,
        height: 10,
        marginRight: 2,
        borderRadius: 1,
      },
    });
