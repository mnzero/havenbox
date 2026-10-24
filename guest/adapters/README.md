# Harness adapter packaging boundary

`hb-adapters` owns native harness protocol translation, not authorization. HB-09 only exposes typed harness identities and an explicit unavailable probe, tested for builtin/Kimi CLI/Pi/dsh. It does not spawn a harness, implement open/submit/events/approval/cancel/close, install dependencies, or emulate successful calls.

Actual adapters and reviewed patches remain HB-20–23 work against the candidate contract in `contracts/protocol.md`. Their Node/Python/SDK lockfiles must be added when real dependencies are selected; there are deliberately no fabricated npm/Python locks here. All four harnesses remain in this release's scope, and unavailable is not integration acceptance.
