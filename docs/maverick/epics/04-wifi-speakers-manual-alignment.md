# Epic 04: Wi-Fi Speakers Manual Alignment

## Goal

Make Wi-Fi speaker support operational and understandable as its own lane by
proving discovery, connection, playback, and manual alignment behavior before
coupling it to microphone-based automation.

## Known Background

Wi-Fi speaker connection and playback have worked historically, but proper
synchronization with Bluetooth speakers has not yet been achieved.

## In Scope

- Wi-Fi speaker discovery and connection flows
- playback fan-out involving Wi-Fi speakers
- manual latency configuration and manual validation
- UI flows that support Wi-Fi speaker management

## Out of Scope

- startup microphone calibration
- runtime ultrasonic auto-correction
- combining Wi-Fi work with microphone automation before manual behavior is
  proven and understood

## Starting Point

Begin from `foundation/neutral-minimal`. Reintroduce only Wi-Fi-specific code
and keep microphone automation out of the lane until manual validation is
complete.

## Validation Expectations

- local backend/frontend checks as applicable
- Raspberry Pi validation is mandatory
- evidence should show Wi-Fi discovery/playback/manual alignment behavior in a
  way that can be repeated before automation is layered on
