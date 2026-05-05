# Epic: ui-polish

_The strategic context lives in [`../ROADMAP.md`](../ROADMAP.md) §3.2 H6. This file describes what counts as in-scope for the UI lane and what's reserved for the audio-engine lanes._

## Goal

Make the Expo / React Native frontend something a non-technical user can use without operator coaching. Today the surface reflects what the audio engine demanded; the work is to reflect what end users need.

## Why This Lane Exists

The current `SpeakerConfigScreen.tsx` was iterated by an engineer who was simultaneously writing the BLE protocol it talks to. It works in the operator's hand because the operator knows what each pill means and what to do when calibration stalls. A stranger looking at it for the first time has no theory of mind for what's happening, what's blocking, what's failing, or what to do next.

## In Scope

- **`SpeakerConfigScreen.tsx` audit.** Button hierarchy, spacing, theming, status pill placement, sequence-progress label, alarm fonts. Clean up the 11 unused-import warnings.
- **Card system.** Today: a single vertical list of speaker cards. Future: group cards by output type (BT vs Wi-Fi) once both are in production use; consider a richer per-card layout that hides advanced controls behind a disclosure.
- **Per-speaker telemetry visualization.** RSSI dip meter, current vs target latency, recent soft-mute events. The `coordinatorState` BLE notifications are already wired in `useBLE.ts` but no component renders them yet.
- **Animation cleanup.** `AnimatedGradient` and other unused visual components in `home.tsx` were imported but never wired — either ship them or remove them.
- **Copy + tone.** Status messages, error strings, confirmation prompts: rewrite for someone who has never read the architecture proposal.
- **Empty / loading / error states.** Every screen needs a deliberate behavior when there's no data, when data is loading, and when something failed.
- **First-time onboarding flow.** Pair-your-first-speaker, run-startup-calibration, here's-what-you're-seeing — coordinated with `feature-hardening` H2 (the Pi-side first-run script).

## Boundaries

- Audio-stack changes (delay engine, coordinator, BLE protocol) belong in `feature-hardening` or `ultrasonic-runtime-sync`. UI polish should not require touching `backend/syncsonic_ble/` except to add a missing notification field if a UI surface genuinely needs it.
- BLE protocol versioning is `feature-hardening` H4, not here — but the UI needs to render the "update needed" banner when the protocol_version mismatch fires, so coordinate.
- Custom hardware industrial design (enclosure shape, panel labels, LED behavior) belongs in `custom-hardware-design`.

## Planning Guidance

- Prefer iterative, single-screen slices. Don't re-platform the app; rewrite one screen at a time with operator review between slices.
- A slice is "done" when the operator runs through the relevant flow without coaching and without checking journalctl. Pi validation is not required here unless the change touches a `useBLE.ts` hook that talks to live hardware.
- Use the existing `AnimatedGradient` and theme tokens before introducing new visual components — match what's already in the codebase.
- Keep the developer tooling pragmatic: lint warnings should reach zero in this epic, not "we'll get to them."
- Visual mockups for any meaningful UI change go in `docs/maverick/proposals/ui-*.md` before implementation. Lessons from the audio side: design first, then build.
