# Epic 04: Wi-Fi Speakers Manual Alignment

> **Status: DONE (2026-05-01).** Wi-Fi speakers (Sonos) ship as
> **auto-aligned** peer engine outputs, not "manual alignment". The
> originally-scoped manual slider workaround was superseded once the
> Slice 4 cross-correlation analyzer proved it could measure the
> ~5 s Sonos lag directly via the chirp anchor pattern. Real-world
> 3-speaker (2 BT + Sonos) alignment confirmed by the project owner
> as "perfectly aligned" 2026-05-01 EDT. Implementation rolled into
> Epic 02; runtime evidence in architecture proposal §18. The W1-W4
> slice plan below was followed in spirit but compressed into the
> single Wi-Fi-anchor delivery rather than a multi-week stagger.

> **Architecture proposal:** [`../proposals/04-wifi-speakers-architecture.md`](../proposals/04-wifi-speakers-architecture.md)
> reframes this lane as a downstream consumer of the Slice 2 elastic
> engine and Slice 4 mic calibration. Read it first.

## Goal

Make Wi-Fi speaker support operational and understandable by reusing the
coordinated engine and mic-driven calibration that landed under
[Epic 05](05-coordinated-engine.md). Wi-Fi outputs become **peer engine
outputs** of Bluetooth ones, not a parallel pipeline.

## Known Background

Wi-Fi speaker connection and playback have worked historically through
an Icecast + FFmpeg producer; proper synchronization with Bluetooth
speakers has not yet been achieved because Wi-Fi delays (~300–1500 ms)
exceed the manual slider's 0–500 ms range. Slice 4's startup chirp
calibration solves that — Wi-Fi just needs to participate in the same
filter / socket layout the calibration sequence walks.

## In Scope

- Wi-Fi speaker discovery and connection flows
- playback fan-out involving Wi-Fi speakers
- manual latency configuration and manual validation
- UI flows that support Wi-Fi speaker management

## Out of Scope

- runtime ultrasonic auto-correction (Epic 03 territory)
- replacing the Icecast/FFmpeg encoder with a custom binary (deferred to
  the SAE Mid horizon)
- group-of-Sonos manipulations beyond ungrouping a target before play

## Slice Plan

| Slice | Outcome | Effort |
|---|---|---|
| W1 | Discovery + presence (read-only) | 2–3 days |
| W2 | Engine output adapter (pipe sink + FFmpeg + Icecast → engine filter) | 1 week |
| W3 | Calibration integration (`startup_tune` covers Wi-Fi lag window) | 3–4 days |
| W4 | Manual fallback + UI parity | 2–3 days |

Detail in [`../proposals/04-wifi-speakers-architecture.md`](../proposals/04-wifi-speakers-architecture.md).

## Starting Point

Branch off `epic/05-coordinated-engine` after Slice 4's runtime
validation lands. The Slice 2/4 surface is the substrate; do not fork
back to `foundation/neutral-minimal`.

## Validation Expectations

- local backend/frontend checks
- Raspberry Pi validation is mandatory
- a 30 s session report with at least one BT and one Wi-Fi output shows
  zero audible drift after a single startup-tune calibration press
