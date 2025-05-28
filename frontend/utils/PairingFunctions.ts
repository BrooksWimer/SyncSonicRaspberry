import { Alert } from 'react-native';

export interface Device {
  mac: string;
  name: string;
}

// Toggle selection of a paired device using its MAC as unique key
export const togglePairedSelection = (
  device: Device,
  currentSelection: Record<string, Device>,
  setSelection: (callback: (prev: Record<string, Device>) => Record<string, Device>) => void
) => {
  setSelection(prev => {
    const newSelection = { ...prev };
    if (newSelection[device.mac]) {
      delete newSelection[device.mac];
    } else {
      // Allow a maximum of three devices
      if (Object.keys(newSelection).length >= 3) {
        Alert.alert('Selection Limit', 'You can select up to 3 devices.');
        return prev;
      }
      newSelection[device.mac] = device;
    }
    return newSelection;
  });
};

// Toggle selection of a device using its MAC as unique key
export const toggleSelection = (
  device: Device,
  currentSelection: Record<string, Device>,
  setSelection: (callback: (prev: Record<string, Device>) => Record<string, Device>) => void
) => {
  setSelection(prev => {
    const newSelection = { ...prev };
    if (newSelection[device.mac]) {
      delete newSelection[device.mac];
    } else {
      // Allow a maximum of three devices
      if (Object.keys(newSelection).length >= 3) {
        Alert.alert('Selection Limit', 'You can select up to 3 devices.');
        return prev;
      }
      newSelection[device.mac] = device;
    }
    return newSelection;
  });
};

// Pair the selected devices by sending them to the Pi's /pair endpoint
export const pairSelectedDevices = async (
  selectedDevices: Record<string, Device>,
  selectedPairedDevices: Record<string, Device>,
  setPairing: (isPairing: boolean) => void,
  configIDParam: string | null,
  configName: string,
  updateConnectionStatus: (id: number, status: number) => void,
  getSpeakers: (id: number) => Device[],
  addSpeaker: (configId: number, name: string, mac: string) => void,
  updateSpeakerConnectionStatus: (configId: number, mac: string, isConnected: boolean) => void,
  addConfiguration: (name: string, callback: (id: number) => void) => void,
  router: any
): Promise<void> => {
  const allSelectedDevices = { ...selectedDevices, ...selectedPairedDevices };
  if (Object.keys(allSelectedDevices).length === 0) {
    Alert.alert('No Devices Selected', 'Please select at least one device to add to configuration.');
    return;
  }

  setPairing(true);
  try {
    if (configIDParam) {
      // For existing configuration, just route to settings/config
      router.replace({
        pathname: '/settings/config',
        params: { 
          configID: configIDParam,
          configName: configName
        }
      });
    } else {
      // Create new configuration and route to settings/config
      addConfiguration(configName, (newConfigID: number) => {
        router.replace({
          pathname: '/settings/config',
          params: { 
            configID: newConfigID.toString(),
            configName: configName
          }
        });
      });
    }
  } catch (error) {
    console.error('Error:', error);
    Alert.alert('Error', 'Failed to create configuration. Please try again.');
  } finally {
    setPairing(false);
  }
};

// Create a new configuration with selected devices
export const createConfiguration = (
  selectedDevices: Record<string, Device>,
  selectedPairedDevices: Record<string, Device>,
  setPairing: (isPairing: boolean) => void,
  configName: string,
  addConfiguration: (name: string, callback: (id: number) => void) => void,
  addSpeaker: (configId: number, name: string, mac: string) => void,
  router: any
): void => {
  const allSelectedDevices = { ...selectedDevices, ...selectedPairedDevices };
  if (Object.keys(allSelectedDevices).length === 0) {
    Alert.alert('No Devices Selected', 'Please select at least one device to add to configuration.');
    return;
  }

  setPairing(true);
  try {
    // Create configuration and navigate to SpeakerConfigScreen
    addConfiguration(configName, (newConfigID: number) => {
      // Add all selected speakers to the new configuration
      Object.entries(allSelectedDevices).forEach(([mac, device]) => {
        addSpeaker(newConfigID, device.name, mac);
      });
      
      // Navigate to SpeakerConfigScreen
      router.replace({
        pathname: '/SpeakerConfigScreen',
        params: { 
          configID: newConfigID.toString(),
          configName: configName
        }
      });
    });
  } catch (error) {
    console.error('Error:', error);
    Alert.alert('Error', 'Failed to create configuration. Please try again.');
  } finally {
    setPairing(false);
  }
};
