# SyncSonic – Multi-Device Bluetooth Audio Hub

## Project Overview and Purpose

**SyncSonic** is a smart system designed to let music lovers play audio seamlessly across multiple Bluetooth speakers or soundbars at once – even if the speakers are different brands. The goal is to break the usual one-device limit of Bluetooth audio and create a **whole-apartment synchronized sound experience**. By using a special hardware hub (a Raspberry Pi) with custom software, SyncSonic can link multiple speakers and calibrate them for optimal sound based on their locations. In practice, this means you can enjoy music in every room with all speakers perfectly in sync, eliminating the hassle of brand-specific solutions and "Bluetooth one-at-a-time" restrictions.

## System Architecture Overview

SyncSonic consists of two main parts working together:

* **Raspberry Pi Hub (Backend):** A Raspberry Pi (with multiple Bluetooth adapters) runs the backend software. This hub is the bridge between the phone’s audio and the Bluetooth speakers. It receives the audio stream from the user’s phone and then simultaneously transmits that audio to all connected Bluetooth speakers. It also hosts a local server to communicate with the mobile app for control commands.
* **Expo Mobile App (Frontend):** A cross-platform mobile application (built with Expo/React Native) serves as the user interface. The app lets users select which speakers to connect, control synchronization settings, and send commands to the Pi hub. The phone’s audio output is integrated such that when you play music on your phone (and direct it to SyncSonic), the Pi will act as the output and forward the music to all chosen speakers.

**How it works (big picture):** The mobile app and Raspberry Pi communicate using Bluetooth Low Eneregy (BLE). The user interacts with the app to pick speakers and adjust settings; the app sends these choices to the Pi’s backend. The Pi then uses its multiple Bluetooth radios to pair with and stream to each speaker concurrently. Under the hood, the Pi uses *one Bluetooth adapter per speaker* to overcome the one-device limit – effectively creating individual connections that share the same audio source. The audio from the phone is captured by the Pi (as if the Pi were a Bluetooth speaker for the phone) and then duplicated out to all connected speaker devices in sync.

## Backend – Raspberry Pi Hub

The backend is the **heart of SyncSonic**, running on a Raspberry Pi configured as a multi-Bluetooth audio hub. It has several key responsibilities:

* **Bluetooth Connection Manager:** The Pi manages multiple Bluetooth connections simultaneously. It uses multiple USB Bluetooth adapters so that each speaker gets its own adapter (enabling parallel A2DP audio streams). The software scans for nearby Bluetooth speakers, handles pairing (if not already paired), and connects to the selected devices. It leverages the BlueZ Bluetooth stack on Linux (and tools like `bluetoothctl` or BlueZ D-Bus APIs behind the scenes) to initiate and maintain these connections.
* **Audio Streaming and Synchronization:** The Pi acts as a Bluetooth audio **sink** for the phone and as a **source** for the speakers. In practice, you pair your phone to the Pi (the Pi identifies itself as an audio receiver). When you play music on your phone, the sound is sent to the Pi. The backend captures this single audio stream and routes it to all active speaker connections at once. To achieve this, the system creates a **virtual combined audio output** that includes all connected Bluetooth speakers, so the music is duplicated to each one. This setup relies on the Pi’s audio subsystem (PulseAudio or a similar sound server) to synchronize output and adjust for any timing differences. (Using multiple adapters avoids the limitation where one Bluetooth transmitter can only handle one audio device at a time.) The backend can also apply per-speaker calibrations – for example, delaying audio on a closer speaker to match a farther speaker, or adjusting volumes – so that the output is **synchronized and balanced** across your space.
* **Control Communication via BLE:** The SyncSonic system originally used a REST API for communication between the mobile app and the Raspberry Pi hub, requiring both devices to be on the same local Wi-Fi network. While functional, this approach limited portability and required users to configure network settings manually. To overcome these limitations, the project transitioned to using Bluetooth Low Energy (BLE) for all control communication. Now, the mobile app interacts with the Raspberry Pi hub directly over BLE by reading and writing to custom characteristics. For example, when a user toggles a speaker on or off, selects devices, or adjusts calibration settings, the app sends a BLE command directly to the Pi. The Pi then handles pairing, connection, or parameter updates and responds with BLE notifications, allowing the app to update the UI in real time with status messages like “Speaker X connected” or “Calibration saved.” This shift to BLE significantly improves usability and portability: no internet connection or network setup is required, making SyncSonic a plug-and-play solution that can be used anywhere—indoors, outdoors, or on the go. It also aligns better with the user expectation that a wireless speaker system should “just work” without setup hurdles.

## Backend Project Structure

The backend is organized into a Python package called `syncsonic_ble` with the following structure:

### Core Components

* **`backend/`** – Top-level directory containing:
  * **`syncsonic_ble/`** – Main Python package containing the core application code
    * **`main.py`** – Entry point that bootstraps the BLE GATT server, connection service, and runs the GLib main loop. It handles initialization of PulseAudio, D-Bus integration, BlueZ adapter selection, and sets up the pairing agent.
    * **`state_management/`** – Contains core state management components:
      * `bus_manager.py` – Manages D-Bus connections
      * `device_manager.py` – Handles Bluetooth device discovery and management
      * `connection_manager.py` – Manages BLE connections and state
    * **`infra/`** – Infrastructure components:
      * `connection_agent.py` – Implements the phone pairing agent
      * `gatt_service.py` – Implements the BLE GATT service and characteristics
    * **`helpers/`** – Utility modules:
      * `pulseaudio_helpers.py` – PulseAudio setup and configuration
      * `adapter_helpers.py` – BlueZ adapter management utilities
    * **`utils/`** – General utilities:
      * `logging_conf.py` – Logging configuration
      * `constants.py` – Application constants and UUIDs
    * **`state_change/`** – Handles state transitions and events

### System Files

* **`syncsonic.service`** – Systemd service file for running the application as a service
* **`start_syncsonic.sh`** – Shell script to start the application
* **`reset_bt_adapters.sh`** – Utility script for resetting Bluetooth adapters
* **`pulse-headless.pa`** – PulseAudio configuration for headless operation

### Architecture Overview

The backend is designed to run as a systemd service on the Raspberry Pi, providing a BLE GATT server that mobile devices can connect to. It uses:
- BlueZ for Bluetooth management
- D-Bus for system communication
- PulseAudio for audio handling

The application follows a modular architecture with clear separation between:
- State management
- Infrastructure components
- Utility functions

This structure reflects the implementation which uses BLE (Bluetooth Low Energy) for device communication. The system is built around a GATT service that handles device discovery, pairing, and audio streaming coordination.

## Frontend – Expo Mobile App

The frontend is a **React Native app (built with Expo)** that provides an intuitive interface for users to control SyncSonic. Its primary role is to let the user interact with the system without dealing with low-level details. Key features of the app include:

* **Device Discovery UI:** When launched, the app can display a list of available Bluetooth audio devices (speakers/soundbars) that the Pi hub can connect to. The app likely obtains this list by calling a backend API (e.g. GET `/devices`) which returns discovered device names, IDs, or statuses. The UI might show each speaker with a toggle or checkbox.
* **Connect/Disconnect Controls:** The user can select which speakers should play the audio. For each device listed, a toggle or button will send a connect or disconnect request. Toggling “on” a speaker triggers the app to call the Pi’s `/connectDevice` endpoint (with the device’s identifier), and toggling “off” calls a `/disconnectDevice`. The app updates the UI to reflect the current connection status of each speaker (e.g. highlighting those currently connected).
* **Calibration and Settings:** SyncSonic’s app may provide a settings screen for audio calibration. For example, the user could set a delay for a specific speaker (if one speaker is physically closer, a slight delay can prevent the sound from that speaker leading the others). The app might present sliders or input fields for each connected speaker’s volume level and delay. When adjusted, the app sends these parameters to the backend (perhaps via a `/calibrate` or `/settings` API call), which in turn applies them to the audio pipeline. This helps achieve the optimal, synchronized sound mentioned in the project’s goals.
* **Status and Feedback:** The app provides feedback to the user. For instance, if a connect command fails or a device is out of range, the app can display an error or notification. When everything is working, the app might show a “Now Playing on X speakers” message, confirming that the audio is streaming to all chosen devices.

## Frontend Architecture

### Project Structure

The mobile app is built using Expo and React Native, with a modern, well-organized structure:

#### App Entry & Navigation
- Uses Expo Router for file-based routing and navigation
- Main screens located in the `app/` directory:
  - `DeviceSelectionScreen.tsx` - Handles Bluetooth device scanning and pairing
  - `SpeakerConfigScreen.tsx` - Manages speaker configurations and settings

#### UI Components & Styling
- Uses Tamagui as the UI component library with built-in theme support
- Components organized in the `components/` directory:
  - `TopBar.tsx` - Common navigation component
- Supports both dark and light themes, automatically switching based on system preferences

#### State Management & Data Flow
- React hooks for state management (`hooks/` directory)
- Contexts managed in `contexts/` directory
- Local data persistence through SQLite database operations

#### Utility Functions
Core functionality organized in the `utils/` directory:
- `PairingFunctions.ts` - Handles BLE device pairing logic
- `SpeakerFunctions.ts` - Manages speaker control operations
- `ConfigurationFunctions.ts` - Handles configuration management

#### Bluetooth Communication
- Direct BLE communication with speakers:
  - Device scanning and discovery
  - Device pairing and connection management
  - Real-time control of speaker settings
  - No API or server communication required

### Key Features

1. **Device Selection & Pairing**
   - BLE device scanning and discovery
   - Direct pairing with Bluetooth speakers
   - Support for multiple speaker configurations

2. **Speaker Control**
   - Individual volume control via BLE
   - Fine-tuned latency control
   - Direct connection management

3. **Configuration Management**
   - Create, save, and delete speaker configurations
   - Automatic saving of settings changes
   - Local storage of configurations

### Project Configuration
- TypeScript for type safety
- Configuration files:
  - `app.config.ts` - Expo configuration
  - `tamagui.config.ts` - UI theme and component configuration
  - `tsconfig.json` - TypeScript configuration

### User Experience Flow

1. **Initial Setup**
   - Connect to the Pi over Classic Bluetooth from phones bluetooth settings
   - Launch app and connect to Pi over Bluetooth Low Eneregy for communication
   - Navigate to device selection screen
   - Scan for available Bluetooth speakers
   - Select and pair desired speakers

3. **Configuration Creation**
   - Create named configurations for different speaker setups
   - Configure individual speaker settings
   - Save configurations for future use

4. **Daily Use**
   - Select saved configuration
   - Adjust volume and latency as needed
   - Connect/disconnect speakers as required

The frontend is designed to be user-friendly and intuitive, handling the complexity of managing multiple Bluetooth speakers through direct BLE communication. This allows users to focus on their audio experience without the need for intermediate servers or APIs.

### Technical Implementation

The app follows modern React Native best practices with:
- Clear separation of concerns
- Type-safe development with TypeScript
- Efficient state management
- Direct BLE communication
- Local data persistence
- Responsive and theme-aware UI

This architecture ensures a smooth, reliable user experience while maintaining the flexibility to support various Bluetooth speaker configurations.




**In summary**, SyncSonic is a novel audio technology that brings synchronized, multi-speaker Bluetooth streaming to the masses—regardless of speaker brand, platform, or environment. Unlike anything currently available on the market, SyncSonic enables users to connect multiple Bluetooth speakers simultaneously and play audio in perfect sync across all of them. It addresses a growing demand for affordable multi-room or shared-group audio experiences, delivering the functionality of premium, brand-locked ecosystems (like Sonos or Apple's AirPlay) at a fraction of the cost. Initially implemented using a Raspberry Pi and multiple Bluetooth adapters, the system pairs with a custom-built mobile app that controls connections and calibration via Bluetooth Low Energy (BLE), requiring no Wi-Fi setup or internet connection. Now that the core system is fully functional, the next step is to develop a custom hardware unit—smaller, cheaper, and optimized for real-world use—turning SyncSonic into a plug-in phone accessory that anyone can carry and use anywhere. This hardware innovation will make true cross-brand, portable, synchronized Bluetooth audio possible for the first time.
