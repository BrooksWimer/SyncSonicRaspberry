# Epic: wifi-manual

A first-class lane for Wi-Fi audio target integration.

## Status

v1 of Wi-Fi speaker integration ships in the coordinated engine on `main`: Sonos peers join the multi-speaker mix as auto-aligned outputs through the same elastic-buffer scheduling that BT speakers use. That is **the floor**, not the ceiling — this lane owns ongoing work to broaden the Wi-Fi target set.

The original epic/04-wifi-speakers-manual-alignment branch (now historical) preserves the v1 development trail.

## Goal

Connect and play through a wider set of Wi-Fi audio targets so the system isn't just "the operator's Sonos soundbar in the operator's living room." Forward work includes:

- **Wi-Fi-connected TVs** as audio sinks (Chromecast Built-in, AirPlay 2, Spotify Connect endpoints) — operator's stated next target
- Other Sonos models and configurations beyond the soundbar tested in v1
- Generic AirPlay 2 receivers
- Other multi-room audio platforms where the Pi can act as a coordinator

## In scope

- Discovery and pairing flow for new Wi-Fi audio target types
- Coordinated-engine integration for each target (latency tuning, codec negotiation, route management)
- Validation evidence on real hardware for each new target
- Documentation of which targets work today and what's required to add a new one

## Out of scope

- BT-only speaker work (lives in the coordinated engine and `feature-hardening`)
- Multi-room spatial coordination (`spatial-audio-awareness`)
- Hardware redesign (`custom-hardware-design`)

## Discord

Lane Discord thread: see `discord_thread_bindings` for the current routing.

## Notes

- The historical `epic/04-wifi-speakers-manual-alignment` branch is preserved as a reference.
- New work on this lane branches from `main`.
