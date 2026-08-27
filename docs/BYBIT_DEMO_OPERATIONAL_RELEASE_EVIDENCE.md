# Bybit Demo operational release evidence

This gate closes the gap between code qualification and real protected operational evidence. It is deliberately read-only: it does not receive Bybit credentials or database credentials, does not run on the protected fixed-egress runner, does not ARM v121, does not create operator approval, and cannot submit or manage an exchange order.

## Why this exists

The operational controls are intentionally split across separate workflows. A green pull request proves code, not activation, and one successful protected workflow does not prove the rest of the chain. Without an additional provenance gate, valid artifacts from different commits or different operational attempts could be mixed manually.

The release-evidence workflow therefore accepts GitHub Actions run IDs, independently reads each run through the GitHub API and requires every supplied source run to be:

- from the expected workflow;
- a manual `workflow_dispatch`;
- `completed` with conclusion `success`;
- bound to the exact `GITHUB_SHA` of the release-evidence run;
- supplied in contiguous operational order;
- non-overlapping with the preceding source run.

Both `run_started_at` and the completed run `updated_at` are retained in sanitized source metadata. ARM, entry and recovery event timestamps must fall inside the corresponding source-run window. This prevents a valid artifact from a different operational episode from being relabeled by a later successful workflow run.

The ARM step is explicit evidence, not an inferred side effect. The control-plane artifact must prove a successful fixed-egress `arm` operation on the same exact Git SHA. Its immutable receipt event ID and `armed_until` must match the `pinned_control_event_id` and `pinned_control_armed_until` in the later operator-approved entry artifact, and the entry observation must occur strictly inside that ARM window.

Run chronology alone is not sufficient for the final recovery stage. The recovery artifact itself must prove a newly created post-entry recovery: its status must be exactly `RECOVERED`, `idempotent_existing_recovery` must be false, and its `created_at` must be strictly later than the operator-approved entry `observed_at`. A later workflow run that merely replays an older `ALREADY_RECOVERED` receipt cannot satisfy the release gate.

The sanitized source-run metadata is hashed and included in the final manifest together with SHA-256 of every source evidence file.

## Stages

The manifest reports the highest stage that is actually proven:

1. `INFRA_READY` — exact-head activation-readiness evidence is clean while v121 remains HALTED.
2. `SESSION_READY` — the durable v122 session exists and is worker-ready.
3. `SUPERVISOR_READY` — one exact-head persistent-supervisor cycle proves the pre-entry state is `IDLE_NO_ACTIVE_TRADE`, with no autonomous, bypass or replacement entry path.
4. `ARM_PROVEN` — a successful exact-head v121 ARM run is bound to its immutable receipt, fixed-egress preflight and short-lived ARM decision.
5. `DEMO_ENTRY_PROVEN` — one explicit operator-approved Demo entry completed inside that exact ARM window with matching pinned control event, immutable authorization/provenance and completed protection reconciliation.
6. `RECOVERY_DRILL_PROVEN` — a newly created controlled v123 `RECOVERED` receipt is present with immutable audit, no automatic takeover and `created_at` strictly later than the proven Demo entry `observed_at`.

Missing later-stage evidence is not treated as a fabricated failure. The manifest remains valid at the highest proven stage and states `next_required_evidence`. Supplying a later artifact while an earlier stage is absent is different: that is a non-contiguous chain and fails closed to `BLOCKED`.

`release_gate_complete=true` exists only at `RECOVERY_DRILL_PROVEN`. It is evidence that the defined Demo operational qualification sequence has been completed for one exact code head; it is not permission for autonomous trading, mainnet order routing, strategy promotion or automatic activation.

## Source artifacts

The gate consumes the sanitized artifacts produced by:

- `bybit-demo-activation-readiness`;
- `bybit-demo-session-start`;
- `bybit-demo-persistent-supervisor`;
- `bybit-demo-control-plane` in `arm` mode;
- `bybit-operator-approved-demo-execution`;
- `bybit-demo-runtime-lease-recovery`.

Every producer artifact is bound to `GITHUB_SHA`. The release gate rejects a source artifact whose embedded Git SHA differs from the exact release-evidence head even if the GitHub run metadata itself appears valid.

The activation-readiness artifact additionally carries its own canonical `manifest_sha256`, which is recomputed before it can advance the chain.

The control-plane receipt and decision must agree on the ARM event ID and expiry. The receipt must be immutable, have no order-submission capability, use a valid fixed-egress preflight digest and represent an ARM TTL within the existing five-minute limit.

The entry stage has a second identity and temporal binding to that exact ARM receipt. This prevents a valid entry artifact from being paired with a different ARM event from the same code head.

The recovery stage has another temporal binding at the artifact level. This prevents a successful later recovery workflow run from reusing an immutable receipt that belongs to an earlier operational episode.

## Safety semantics

The gate does not incorrectly require all source workflows to be incapable of exchange writes. Their responsibilities differ:

- activation readiness, session start, control-plane evidence and lease recovery expose no exchange order-write capability;
- the control-plane ARM step changes only the append-only v121 new-entry authorization state and never submits an order;
- the persistent supervisor may tighten protection or perform deterministic reduce-only safety exits for an already-open Demo trade, but may not open a new trade or bypass operator approval;
- the operational-entry path may perform at most the one explicitly approved Demo ENTRY, but may not auto-ARM, select a fallback opportunity, submit a same-invocation replacement entry or route mainnet orders.

The final release-evidence workflow itself always reports:

- `automatic_activation_allowed=false`;
- `order_write_performed=false`;
- `order_writes_supported=false`;
- `live_mainnet_order_routing_allowed=false`.

## Operational use

Run the protected source workflows in strict order on the same deployable Git SHA: readiness, durable session, idle supervisor proof, explicit short-lived ARM, one operator-approved Demo entry, then the controlled recovery drill. Then dispatch `bybit-demo-operational-release-evidence` on that same SHA and provide the corresponding run IDs from the first stage through the last stage currently available.

Do not skip ARM and do not substitute an old ARM artifact just because its workflow ran on the same commit. Do not substitute a pull-request qualification run for an operational source run. Do not substitute a lease-recovery inspection artifact or an `ALREADY_RECOVERED` replay for the exact newly created `RECOVERED` receipt from the post-entry recovery drill.

No operational workflow dispatch is performed automatically by this gate or by pull-request CI.
