# Epic 02: Startup Mic Auto-Alignment

## Goal

Add a startup-only microphone calibration flow that uses human-audible probe
sounds to measure end-to-end latency and publish clean alignment targets into
the stable actuation path.

## In Scope

- startup probe generation and capture
- latency estimation at boot or startup
- publishing startup targets through the shared control-plane/actuation layer
- calibration UX or operator guidance tied to startup alignment

## Out of Scope

- runtime ultrasonic correction while music is already playing
- Wi-Fi speaker feature work
- unrelated PipeWire transport experiments outside what startup calibration
  strictly needs

## Starting Point

Begin from `foundation/neutral-minimal`. Reintroduce only the startup mic
pieces needed for startup calibration, not the broader experimental stack.

## Validation Expectations

- local backend/frontend checks as applicable
- Raspberry Pi validation is mandatory
- evidence should show the startup flow can measure and apply alignment without
  destabilizing the baseline runtime
