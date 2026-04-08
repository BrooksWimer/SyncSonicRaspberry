# Epic 03: Runtime Ultrasonic Auto-Alignment

## Goal

Support runtime microphone-based correction using ultrasonic bursts while music
is playing, without disrupting the listening experience or bypassing the shared
actuation boundary.

## In Scope

- ultrasonic burst generation and capture
- runtime measurement while playback is active
- bounded runtime correction logic
- UX and reporting specific to runtime auto-alignment

## Out of Scope

- startup audible calibration
- Wi-Fi speaker discovery/connection work
- unrelated transport refactors unless they are strictly required for runtime
  correction to function safely

## Starting Point

Begin from `foundation/neutral-minimal`. Reintroduce only runtime ultrasonic
behavior needed for this lane and keep it isolated from startup calibration.

## Validation Expectations

- local backend/frontend checks as applicable
- Raspberry Pi validation is mandatory
- evidence should show that runtime correction works during playback and does
  not degrade the stable baseline experience
