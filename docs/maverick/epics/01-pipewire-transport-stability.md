# Epic 01: PipeWire Transport Stability

## Goal

Improve the stability and predictability of SyncSonic's PipeWire delay-node
transport and manual latency application without pulling unrelated microphone
or Wi-Fi work into the lane.

## In Scope

- delay-node route creation and teardown
- control-plane semantics for manual delay and output mix
- actuation boundary behavior and delay translation
- deterministic PipeWire daemon/runtime behavior
- transport observability or maintenance only when it serves a concrete
  validation goal

## Out of Scope

- startup microphone calibration
- runtime ultrasonic measurement/correction
- Wi-Fi speaker discovery or connection work
- broad speculative infrastructure with no validation path

## Starting Point

Begin from `foundation/neutral-minimal` and review
`backend/FOUNDATION_REORG_NOTES.md` before reintroducing stripped PipeWire
modules or ideas.

## Validation Expectations

- local backend syntax/test checks
- Pi validation for service/runtime behavior
- clear evidence that changes improve or preserve transport stability
- no reintroduction of advanced PipeWire scaffolding without a concrete reason
