# Epic: spatial-audio-awareness

_Exploratory lane. Lowest priority of the six durable epics. The strategic framing lives in [`../ROADMAP.md`](../ROADMAP.md) §3.4. Read that before drafting a slice — and read the operator's "much more complicated and not at all necessary for MVP" framing while you're there._

## Goal

Open-ended research into using microphone-driven room mapping, listener-position awareness, and per-speaker channel routing to enhance the listening experience beyond what stereo synchronization alone can deliver.

## Why This Lane Exists

The coordinated engine currently sends the same stereo mix to every speaker. The operator has flagged spatial audio as a long-tail product expansion that's interesting but not urgent. This epic exists so research scratch-work has a home — not so it competes with `feature-hardening`, `ui-polish`, or `ultrasonic-runtime-sync` for time.

## In Scope (open-ended)

- **Per-speaker channel routing.** Today every speaker plays the stereo mix. Future: assign speaker A as front-left, speaker B as rear-right, etc. Requires app-side speaker placement awareness (drag speakers onto a room layout?) and engine-side per-output channel selection.
- **Microphone-driven room geometry.** The startup mic already measures lag-per-speaker; in principle it could also measure relative speaker position via time-of-arrival. Feasibility study first.
- **Listener-position awareness.** If the operator's phone is the listener-position proxy, can we adjust per-speaker level and delay based on where the phone is in the room? BLE RSSI alone is too coarse; UWB ranging or audio TOA-based approaches are speculative.
- **Spatial format support.** Atmos / Dolby Surround decoding into N output speakers via the elastic engine. Big lift; would require new signal-flow primitives.
- **Home theater / TV setup.** HDMI ARC/eARC into the Pi as an additional input, lip-sync against video. Outside the Bluetooth-speaker product but adjacent.

## Boundaries

- This epic doesn't compete for engineering time with `feature-hardening`, `ui-polish`, `ultrasonic-runtime-sync`, `custom-hardware-design`, or `patent-application`. Slices here happen on operator initiative and are explicitly low-priority.
- The Mid-horizon SAE Rust rewrite (`ROADMAP.md` §3.5) is independent. SAE work is not in this epic.
- This is not a multi-room audio epic. Multi-room (one zone playing music A, another playing music B) is a different product feature and would get its own epic if undertaken.
- Don't add spatial-audio infrastructure to the engine on `main` from this epic. Any engine-level change goes through the regular doctrine: workstream branch from `main`, attached to this epic, finished into the epic branch, then explicitly promoted.

## Planning Guidance

- Default cadence: a slice every 1-2 months at most, on operator request.
- First slice should be a feasibility study, not implementation. Scope: "given the Pi 4 hardware + 1 USB mic + 3 speakers, what spatial features are physically measurable and what's pure research?"
- Slices in this lane often produce documentation rather than code — and that's fine. Capture in `docs/maverick/proposals/spatial-*.md`.
- A slice is "done" when it produces a deliberate go / no-go decision with documented reasoning. "Promising, defer to next slice" is also a valid outcome.
- Decisions made here go into [`PROJECT_MEMORY.md`](../PROJECT_MEMORY.md) so they're durable. Don't bury experimental conclusions in slice transcripts.
