# SyncSonic Project Context

SyncSonic is a Raspberry Pi-based audio hub that synchronizes playback across mixed-brand Bluetooth and Wi-Fi speakers from a phone. The deployable product is a small box you plug speakers into; it makes a "motley collection of off-brand speakers feel like a single coherent audio environment."

## Where this project is

As of 2026-05-05, the **North Star is reached** on the current PipeWire stack: 3-speaker (2 BT + 1 Wi-Fi Sonos) end-to-end auto-aligned playback works on the Pi at 10.0.0.89. The system handles startup calibration (chirp/music anchor), pipeline stability under transport stress, and during-playback correction by soft-mute + phase-aligned re-entry. Five epics (`epic/01..05`) carried that work; their content has been merged into a new `main` branch on 2026-05-05 EDT, and they are retained as historical references in git.

Forward work splits into **ten durable lanes** (see [`PROJECT_ROADMAP.md`](PROJECT_ROADMAP.md), [`WORKSTREAM_MODEL.md`](WORKSTREAM_MODEL.md) for the full table, and the 2026-05-09 entry in [`PROJECT_MEMORY.md`](PROJECT_MEMORY.md) for the elevation history):

- **Six post-North-Star lanes:** `feature-hardening`, `ui-polish`, `custom-hardware-design`, `patent-application`, `ultrasonic-runtime-sync`, `spatial-audio-awareness`.
- **Four lanes elevated from historical epics on 2026-05-09:** `pipewire-stability`, `startup-mic`, `runtime-ultrasonic`, `wifi-manual`. Their v1 implementations shipped via the coordinated engine merge into `main`; each lane now owns ongoing forward work on its slice of the engine.

All ten are first-class peers branching off `main`; the old `epic/01..05` branches stay in git as historical references. Maverick's `config/control-plane.shared.json` registers all ten as `epicBranches` for the `syncsonic` project.

## Operating Model

- Production / default base branch: **`main`** (created 2026-05-05 from `epic/05-coordinated-engine`, Pi-validated identical to deployed reality).
- Each new workstream branches from `main`, attaches to one of the **ten durable lanes**, finishes back into the lane branch, and only the lane gets explicitly promoted to `main`.
- Every change touching audio routing, BLE, latency, or service startup requires Pi validation evidence on `syncsonic@10.0.0.89`. Local checks (`compileall`, lint) are necessary but never sufficient.
- Telemetry retention pattern: `/home/syncsonic/syncsonic-telemetry/` events are durable; pre-deploy `tar -czf` snapshots of `/home/syncsonic/SyncSonicPi/backend/` are the rollback artifact.

## Stack

- Backend: Python (BLE GATT server via `bluez`, audio routing via PipeWire + custom `pw_delay_filter`, coordinator process owning per-speaker state).
- Frontend: Expo / React Native (`SpeakerConfigScreen.tsx` is the primary surface).
- Hardware: Raspberry Pi 4 + 4× USB Bluetooth controllers + USB measurement microphone, optionally 1× Wi-Fi Sonos as anchor.
- Production deploy: systemd service `syncsonic.service` on the Pi, manual `tar`-snapshot deploys (no CI pipeline yet — this is on the `feature-hardening` lane to formalize).

## Strategic horizons (from `ROADMAP.md`)

- **Now** — closed. Coordinated engine slices 0-4 + Wi-Fi anchor + 3-speaker auto-align. North Star reached.
- **Now+** — active. Hardening for industry-readiness, optional ultrasonic-runtime lane, exploratory spatial.
- **Mid** — optional. SyncSonic Audio Engine (SAE) Rust rewrite of the engine layer. Only entered if PipeWire path proves insufficient.
- **Long** — optional. Custom hardware (Pi CM5 carrier or true embedded SoC) + Buildroot/Yocto OS. Only entered if commercial intent is real.

## What this project is not

- Not a streaming service, not a music recommender, not a smart speaker assistant.
- Not multi-room independent audio (yet — see `spatial-audio-awareness` for the optional exploration).
- Not a generic Bluetooth Linux audio fix; the value is the specific multi-speaker synchronization story.

## Cross-references

- [`ROADMAP.md`](ROADMAP.md) — 469-line strategic doc, North Star, design principles, three-horizon plan, open questions. **Read this before starting any non-trivial slice.**
- [`WORKSTREAM_MODEL.md`](WORKSTREAM_MODEL.md) — branch model and historical foundation conventions.
- [`proposals/05-coordinated-engine-architecture.md`](proposals/05-coordinated-engine-architecture.md) — full architecture, 7 root-cause failure modes, slice plan, Pi validation evidence.
- [`epics/01..05-*.md`](epics/) — historical epic charters; preserved for context, no longer the active lane structure.
- [`AGENTS.md`](../../AGENTS.md) — orchestration doctrine and verification baseline that every workstream has to meet.
