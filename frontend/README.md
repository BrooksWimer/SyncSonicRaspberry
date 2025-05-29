# SyncSonicPi Frontend

## Overview
The frontend of SyncSonicPi is built using React Native with Expo, providing a cross-platform mobile application for controlling and interacting with Sonic Pi. The application is written in TypeScript and uses Tamagui for styling and UI components.

## Tech Stack
- **Framework**: React Native with Expo
- **Language**: TypeScript
- **Styling**: Tamagui
- **State Management**: React Context API
- **Development Environment**: Expo

## Project Structure
```
frontend/
├── app/              # Main application screens and navigation
├── assets/           # Static assets (images, fonts, etc.)
├── components/       # Reusable UI components
├── contexts/         # React Context providers
├── hooks/            # Custom React hooks
├── styles/           # Global styles and theme configurations
├── utils/            # Utility functions and helpers
├── app.config.ts     # Expo configuration
├── tamagui.config.ts # Tamagui theme and configuration
└── tsconfig.json     # TypeScript configuration
```

## Getting Started

### Prerequisites
- Node.js (LTS version)
- npm or yarn
- Expo CLI (`npm install -g expo-cli`)

### Installation
1. Navigate to the frontend directory:
   ```bash
   cd frontend
   ```

2. Install dependencies:
   ```bash
   npm install
   # or
   yarn install
   ```

3. Start the development server:
   ```bash
   npm start
   # or
   yarn start
   ```

### Development
- Use `npm start` or `yarn start` to start the Expo development server
- Press `a` to open on Android emulator
- Press `i` to open on iOS simulator
- Scan the QR code with Expo Go app on your physical device

## Features
- Cross-platform mobile application
- Real-time communication with Sonic Pi
- Modern UI with Tamagui components
- Type-safe development with TypeScript

## Building for Production
To create a production build:

```bash
# For Android
expo build:android

# For iOS
expo build:ios
```

## Contributing
1. Create a new branch for your feature
2. Make your changes
3. Submit a pull request

## License
[Add your license information here] 