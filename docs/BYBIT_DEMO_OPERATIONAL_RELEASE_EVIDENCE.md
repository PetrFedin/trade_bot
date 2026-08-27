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
- chronologically later than the preceding stage.

The sanitized source-run metadata is hashed and included in the final manifest together with SHA-256 of every source evidence file.

## Stages

The manifest reports the highest stage that is actually proven:

1. `INFRA_READY` — exact-head activation-readiness evidence is clean while v121 remains HALTED.
2. `SESSION_READY` — the durable v122 session exists and is worker-ready.
3. `SUPERVISOR_READY` — one exact-head persistent-supervisor cycle proves the pre-entry state is `IDLE_NO_ACTIVE_TRADE`, with no autonomous, bypass or replacement entry path.
4. `DEMO_ENTRY_PROVEN` — one explicit operator-approved Demo entry completed with immutable authorization/provenance and completed protection reconciliation.
5. `RECOVERY_DRILL_PROVEN` — a later controlled v123 lease-recovery receipt is present with immutable audit and no automatic takeover.

Missing later-stage evidence is not treated as a fabricated failure. The manifest remains valid at the highest proven stage and states `next_required_evidence`. Supplying a later artifact while an earlier stage is absent is different: that is a non-contiguous chain and fails closed to `BLOCKED`.

`release_gate_complete=true` exists only at `RECOVERY_DRILL_PROVEN`. It is evidence that the defined Demo operational qualification sequence has been completed for one exact code head; it is not permission for autonomous trading, mainnet order routing, strategy promotion or automatic activation.

## Source artifacts

The gate consumes the sanitized artifacts produced by:

- `bybit-demo-activation-readiness`;
- `bybit-demo-session-start`;
- `bybit-demo-persistent-supervisor`;
- `bybit-operator-approved-demo-execution`;
- `bybit-demo-runtime-lease-recovery`.

Every producer artifact is bound to `GITHUB_SHA`. The release gate rejects a source artifact whose embedded Git SHA differs from the exact release-evidence head even if the GitHub run metadata itself appears valid.

The activation-readiness artifact additionally carries its own canonical `manifest_sha256`, which is recomputed before it can advance the chain.

## Safety semantics

The gate does not incorrectly require all source workflows to be incapable of exchange writes. Their responsibilities differ:

- activation readiness, session start and lease recovery must expose no order-write capability;
- the persistent supervisor may tighten protection or perform deterministic reduce-only safety exits for an already-open Demo trade, but may not open a new trade or bypass operator approval;
- the operational-entry path may perform at most the one explicitly approved Demo ENTRY, but may not auto-ARM, select a fallback opportunity, submit a same-invocation replacement entry or route mainnet orders.

The final release-evidence workflow itself always reports:

- `automatic_activation_allowed=false`;
- `order_write_performed=false`;
- `order_writes_supported=false`;
- `live_mainnet_order_routing_allowed=false`.

## Operational use

Run the protected source workflows in strict order on the same deployable Git SHA. Then dispatch `bybit-demo-operational-release-evidence` on that same SHA and provide the corresponding run IDs from the first stage through the last stage currently available.

Do not skip a stage. Do not substitute a pull-request qualification run for an operational source run. Do not substitute a lease-recovery inspection artifact for a real `RECOVERED` / `ALREADY_RECOVERED` recovery receipt.

No operational workflow dispatch is performed automatically by this gate or by pull-request CI.
