# Bybit Demo operational release evidence

This gate closes the gap between code qualification and real protected operational evidence. It is deliberately read-only: it does not receive Bybit credentials or database credentials, does not run on the protected fixed-egress runner, does not ARM or HALT v121, does not create operator approval, and cannot submit or manage an exchange order.

## Why this exists

The operational controls are intentionally split across separate workflows. A green pull request proves code, not activation, and one successful protected workflow does not prove the rest of the chain. Without an additional provenance gate, valid artifacts from different commits or different operational attempts could be mixed manually.

The release-evidence workflow accepts GitHub Actions run IDs and requires every supplied source run to be the expected workflow, a successful manual `workflow_dispatch`, bound to the exact `GITHUB_SHA`, contiguous, and non-overlapping with the preceding source run.

ARM, entry, HALT and recovery event timestamps must fall inside the corresponding source-run window. This prevents a valid artifact from a different operational episode from being relabeled by a later successful workflow run.

The ARM step is explicit evidence. Its immutable receipt event ID and `armed_until` must match the entry artifact's `pinned_control_event_id` and `pinned_control_armed_until`, and entry must occur strictly inside that ARM window.

The HALT step is equally explicit. After entry, a later exact-head control-plane run must prove `HALT_NEW_ENTRIES`. Its immutable event ID must match the `control_event_id` in the subsequent v123 recovery receipt. The HALT timestamp must be strictly later than entry and belong to the exact HALT workflow run.

The final recovery must be newly created after HALT: status exactly `RECOVERED`, `idempotent_existing_recovery=false`, `created_at` strictly later than entry and HALT, and `control_event_id` exactly equal to the HALT event ID. An `ALREADY_RECOVERED` replay cannot complete the gate.

For a full entry-recovery drill the receipt must additionally prove that an active v119 checkpoint existed when the lease was recovered. v123 already stores `active_checkpoint_entry_order_link_id_sha256`; the release gate compares that immutable digest with `SHA256(entry_order_link_id)` from the exact operator-approved entry artifact. A recovery of another checkpoint, a recovery with no checkpoint, or an invalid/missing checkpoint digest fails closed. This requirement applies only to `RECOVERY_DRILL_PROVEN`: controlled lease recovery itself remains valid for legitimate orphaned leases that do not have an active checkpoint.

## Stages

1. `INFRA_READY` — exact-head readiness evidence is clean while v121 remains HALTED.
2. `SESSION_READY` — durable v122 session exists and is worker-ready.
3. `SUPERVISOR_READY` — pre-entry supervisor is `IDLE_NO_ACTIVE_TRADE`, with no autonomous, bypass or replacement entry path.
4. `ARM_PROVEN` — exact-head v121 ARM is bound to immutable receipt and short-lived decision.
5. `DEMO_ENTRY_PROVEN` — one explicit operator-approved Demo entry completed inside that exact ARM window with immutable authorization/provenance and completed protection reconciliation.
6. `HALT_PROVEN` — a later exact-head v121 HALT disables new entries again and exposes the immutable event recovery must consume.
7. `RECOVERY_DRILL_PROVEN` — a newly created controlled v123 `RECOVERED` receipt is bound to that exact HALT event and to the exact entry checkpoint identity.

Missing later-stage evidence leaves a valid partial chain with `next_required_evidence`. Supplying a later artifact while an earlier stage is absent fails closed as a non-contiguous chain. `release_gate_complete=true` exists only at `RECOVERY_DRILL_PROVEN` and is not permission for autonomous trading, mainnet order routing, strategy promotion or automatic activation.

## Source artifacts

The gate consumes sanitized artifacts in strict order from:

- `bybit-demo-activation-readiness`;
- `bybit-demo-session-start`;
- `bybit-demo-persistent-supervisor`;
- `bybit-demo-control-plane` in `arm` mode;
- `bybit-operator-approved-demo-execution`;
- `bybit-demo-control-plane` in `halt` mode;
- `bybit-demo-runtime-lease-recovery`.

Every producer artifact is bound to `GITHUB_SHA`. Readiness also carries a canonical `manifest_sha256`. ARM and HALT receipts are immutable control evidence and expose no order-submission capability. Entry is bound to the exact ARM event; recovery is bound to the exact HALT event and, for a completed release drill, to the exact active checkpoint derived from that entry's `entry_order_link_id`.

## Safety semantics

- activation readiness, session start, control-plane evidence and lease recovery expose no exchange order-write capability;
- ARM and HALT change only append-only v121 new-entry authorization state and never submit an order;
- the persistent supervisor may tighten protection or perform deterministic reduce-only safety exits for an already-open Demo trade, but may not open a new trade or bypass operator approval;
- the operational-entry path may perform at most the one explicitly approved Demo ENTRY, but may not auto-ARM, select a fallback opportunity, submit a replacement entry or route mainnet orders;
- the checkpoint identity used by the release gate is a SHA-256 digest only; the v123 audit remains immutable and no new database write surface is introduced;
- the final release-evidence workflow itself always has `automatic_activation_allowed=false`, `order_write_performed=false`, `order_writes_supported=false` and `live_mainnet_order_routing_allowed=false`.

## Operational use

Run the protected source workflows in strict order on the same deployable Git SHA: readiness, durable session, idle supervisor proof, short-lived ARM, one operator-approved Demo entry, explicit HALT, then controlled recovery. Dispatch the read-only release-evidence workflow only after those source runs exist.

For the final recovery drill, the recovered lease must still be associated with the active checkpoint produced by the exact approved entry. If the checkpoint has already been terminally handed off or belongs to another entry, the recovery operation may still be valid operationally, but it cannot be used as `RECOVERY_DRILL_PROVEN` evidence for this release chain.

Do not skip ARM or HALT, do not substitute old control artifacts, and do not substitute an inspection or `ALREADY_RECOVERED` replay for the exact new post-HALT recovery receipt. No operational workflow dispatch is performed automatically by this gate or by pull-request CI.
