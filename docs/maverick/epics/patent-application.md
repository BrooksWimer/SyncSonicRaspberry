# Epic: patent-application

_Notes-and-prose lane. No transport changes, no Pi deployments, no audio-engine code. The strategic context — what makes this technology novel — lives in [`../ROADMAP.md`](../ROADMAP.md) §4 (design principles) and [`../proposals/05-coordinated-engine-architecture.md`](../proposals/05-coordinated-engine-architecture.md)._

## Goal

Protect the IP behind SyncSonic's coordinated-engine approach to off-brand multi-speaker synchronization before any commercial conversation. Either file a patent application, or arrive at a deliberate "we won't file" decision with documented reasoning.

## Why This Lane Exists

The combination of bounded ±50 ppm rate adjustment, soft-mute + phase-aligned re-entry on transport failure, in-process coordinator owning per-speaker state, and chirp / Wi-Fi anchor calibration is not the way most off-the-shelf BT speaker systems handle drift. It's not obvious this is patentable, but it's also not obvious it isn't, and the operator already has prior research and architecture documentation that would form the substrate of a claim.

## In Scope

- **Inventorship inventory.** Catalog the specific mechanisms in the coordinated engine that are claimable: bounded rate-adjustment policy, soft-mute re-entry, system-coordinator-owned state, mic-driven anchor with chirp/music selectivity, Wi-Fi anchor lag measurement, telemetry-backed convergence.
- **Prior art search.** What does the existing literature look like? Sonos, Bose, Apple, Bluetooth SIG papers, multi-room audio patents. Either commission a search or do an honest first-pass yourself.
- **Claims drafting.** Translate the architecture into independent + dependent claims. This needs a patent attorney's sign-off, not a draft from an LLM.
- **Supporting documentation pack.** Pi validation evidence (already in `proposals/05-coordinated-engine-architecture.md` §8-18), telemetry session reports, code references with permanent commit hashes. The pack should be sufficient to give an attorney everything they need without further questions.
- **File-or-not decision.** Either submit, or write up why submitting isn't worth the cost / disclosure tradeoff.

## Boundaries

- This epic does not change any code in `backend/` or `frontend/`. If a slice ends up needing to add an explanatory comment in source, that's fine, but no transport / engine / UI changes.
- The hardware design — antenna layout, SoC choice, enclosure — is `custom-hardware-design`. Hardware claims (if any) get coordinated across the two epics but the application drafting itself happens here.
- Trade-secret strategy (keep mechanisms internal, don't publish) is an alternative to patenting; it's in scope to discuss as part of the file-or-not decision but is otherwise outside the engineering roadmap.
- Going-to-market work (legal entity, licensing, commercial partner negotiations) is downstream and out of scope.

## Planning Guidance

- Slices in this lane are document deliverables with named consumers (the patent attorney, the operator's future self). Each slice ships a markdown file under `docs/maverick/patent/` or a polished prose section that gets reviewed.
- Don't draft claims with an LLM — at most use one as a starting structure. The claims are legal artifacts, not engineering ones.
- Before submitting anything, do a sanity check pass with someone who has filed a software/hardware patent before. The operator does not have this expertise; budget for outside review.
- Keep all reference material under `docs/maverick/patent/`. Treat it as durable context — operator can edit, planning agents read, future slices reuse.
