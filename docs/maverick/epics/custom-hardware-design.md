# Epic: custom-hardware-design

_The strategic context lives in [`../ROADMAP.md`](../ROADMAP.md) §3.6. This file describes what counts as in-scope for the custom-hardware lane._

## Goal

Produce a sellable form factor for SyncSonic. Replace the Pi 4 + USB hub on a desk with a compact, manufacturable, FCC/CE-pre-scanned reference design. Mostly research, design, and documentation — not Pi-deploying.

## Why This Lane Exists

The coordinated engine is hardware-agnostic enough that the Pi 4 development form factor is fine for the operator's own use. Selling SyncSonic requires a design that ships in a box. The operator has flagged "custom hardware" as a Long-horizon-but-eventually-needed lane; this epic is where hardware research, BOM exploration, and design-review iteration live.

## In Scope

- **SoC + carrier design.** Pi CM5 (or CM4 if CM5 supply slips) on a custom carrier. Soldered BT modules instead of USB hub. Soldered USB measurement mic. Headphone jack? Optical out? Wi-Fi for control plane only or also for Sonos-class streaming?
- **Antenna placement + RF.** 4× BT radios + Wi-Fi + the Pi's own onboard radios in close proximity is a co-existence problem. Pre-scan FCC/CE, document the layout, decide whether external antennas are needed.
- **Thermals + enclosure.** Pi CM5 sustained load with 4 BT controllers active: thermal envelope, fan vs passive, enclosure thermal path. Industrial design: not a Raspberry Pi case, not an Etsy enclosure, something that looks like a product.
- **BOM + costing.** First-pass component costs at 100 / 1k / 10k unit volumes. Decision input for "is this commercially viable at the price the operator is willing to charge?"
- **OS strategy.** Buildroot vs Yocto vs Raspberry Pi OS Lite. RT-PREEMPT kernel? Read-only rootfs with overlay? OTA update mechanism? Decision point: stay with full Linux or move toward an embedded-only firmware model.
- **Decision gate at the end.** Stay CM-class, or jump to a true embedded SoC (AM62x, i.MX RT class) and a much larger firmware project. Default is "stay CM-class" unless cost or supply forces otherwise.

## Boundaries

- Software-side hardening (service lifecycle, recovery, telemetry) is `feature-hardening`. This epic only changes software when the new hardware demands it (different controller layout, different mic, different OS).
- Going-to-market work — legal, channel, manufacturing partner, packaging, retail — is downstream of the design gate at the end of this epic. Out of scope here.
- The Mid-horizon SAE Rust rewrite (see `ROADMAP.md` §3.5) is independent of this epic; SAE is engine-only and runs unchanged on a custom carrier. Don't conflate the two.

## Planning Guidance

- This is a notes-and-research lane more than a code lane. Slices look like: "draft a carrier block diagram", "build a BOM in CSV with unit prices", "do the FCC/CE intent-to-test paperwork research", "summarize three CM5-vs-CM4 supply scenarios". Most slices have no Pi validation step.
- Use `docs/maverick/proposals/hardware-*.md` for the long-form design docs that come out of this epic.
- Decisions get captured in `PROJECT_MEMORY.md` so they're durable across slices.
- Before committing to any vendor, supply chain choice, or component family, write up the alternatives considered and why this one won.
- The operator already has informal patent material relevant to the engine; coordinate with [`patent-application`](patent-application.md) when the hardware design choices need to be referenced as enabling structure for a claim.
