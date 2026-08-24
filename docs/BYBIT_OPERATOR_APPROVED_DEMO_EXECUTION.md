# Operator-approved Bybit Demo execution

This layer connects the evidence-ranked review queue to the canonical Bybit Demo execution stack without enabling autonomous real-money trading.

## Boundary

Mainnet remains read-only. There is no mainnet order client, no mainnet order mutation, and no path that turns evidence rows directly into autonomous orders.

A new Demo entry now requires **both**:

1. the existing short-lived exact operator approval for one evidence-ranked decision; and
2. a separate short-lived v121 `ARM_NEW_ENTRIES` control state created from a fresh connected read-only preflight.

These capabilities solve different problems. Approval binds the exact trade identity/economics. v121 ARM is the operational kill-switch boundary for whether any new Demo exposure may be opened at all.

## Canonical flow

```text
latest positive-evidence review queue
-> operator chooses exact evidence rank / symbol
-> reproduce the fixed signal and source economics
-> exact approval confirmation
-> <= 2 minute BYBIT_DEMO trade approval
-> PostgreSQL v119-v121 VERIFIED_READY
-> connected read-only Demo preflight
-> explicit short-lived v121 ARM
-> canonical single-writer runtime lease
-> durable active-trade checkpoint check
-> if active checkpoint: manage/reconcile existing trade; no new-entry ARM required
-> if no checkpoint: Gate 1 rechecks v121 ARM under the lease
-> latest review-row identity recheck
-> current canonical selector independently chooses the same signal
-> demo wallet / margin / session ledger / previous-trade reconciliation
-> fresh quote + execution-risk resize
-> exact-identity single-use approval guard
-> exact non-reduce-only entry reaches pre-network boundary
-> immutable authorization lineage is persisted and burned
-> Gate 2 rechecks v121 ARM immediately before network submit
-> raw api-demo.bybit.com order/create mutation
-> guarded protection / recovery / accounting
-> immutable protected-entry provenance on the same orderLinkId
-> restart-safe active-trade management
-> terminal evidence + fees + funding + realized PnL
-> approved-trade attribution on the same orderLinkId
```

If any identity/economic check changes the symbol, side, decision, source snapshot, ranks or approved caps, the entry is rejected rather than rerouted. Ranked fallback is not permitted after approval.

## Approval creation

Preparation itself never sends an order:

```bash
python -m tools.prepare_bybit_demo_operator_approval \
  --site eu \
  --evidence-rank 1 \
  --symbol BTCUSDT \
  --confirm APPROVE_BYBIT_DEMO_EXECUTION \
  --output artifacts/bybit-demo-approval.json
```

The approval remains outcome-free and short-lived. It is refused when historical evidence is not sufficiently positive, the source decision cannot be reproduced, source economics drift, activation flags become unsafe, the confirmation phrase is wrong, the signal is stale, or the TTL exceeds its existing bound.

Approval is **not** ARM. A valid trade approval cannot bypass a HALTED v121 control plane.

## v121 new-entry control

See `docs/BYBIT_DEMO_CONTROL_PLANE.md` for the full contract.

Important properties:

- no control event means HALT;
- missing/invalid/expired v121 state means HALT;
- ARM max TTL is 300 seconds, default 120 seconds;
- ARM can only be persisted from exact `READY_FOR_MANUAL_OPERATOR_APPROVAL` connected preflight;
- `EXISTING_TRADE_MANAGEMENT_REQUIRED` can never ARM a new entry;
- ARM persists sanitized canonical preflight evidence plus SHA-256 in append-only PostgreSQL;
- the ARM transaction refuses active v119 runtime lease/checkpoint state;
- HALT blocks new non-reduce-only exposure, not protection/reduce-only recovery of an existing trade.

## Canonical single-writer runtime

`run_operator_approved_bybit_demo_trading_runtime` continues to use `run_bybit_demo_trading_runtime`; there is still only one lifecycle engine.

The canonical runtime remains authoritative for lease acquisition, durable checkpoint inspection, entry-versus-management routing, protected-entry provenance, restart-safe polling, terminal evidence handoff and the prohibition on a replacement entry in the same invocation.

The v121 control plane is intentionally consulted only inside the **new-entry closure**. If a valid active checkpoint exists, the closure is never invoked, so an operator HALT cannot prevent risk reduction or terminal reconciliation of an already-open Demo position.

If no control-plane object is supplied, that closure fails closed before authorization persistence.

## Durable pre-submit authorization lineage

The immutable authorization remains written as late as possible while still preceding the exchange entry mutation.

The relevant client ordering is now:

```text
canonical trading runtime
-> approved account-sized bridge
-> exact approval / identity guard
-> DurableApprovalLineageBybitDemoClient
   (persist and burn immutable pre-submit authorization)
-> ControlPlaneGuardedBybitDemoClient
   (Gate 2: fresh ARM check for non-reduce-only entry)
-> raw BybitDemoOrderClient
   (api-demo.bybit.com HTTP mutation)
```

This order is deliberate. Account, fee, session-risk, fresh-quote, quantity and other pre-order checks may reject the candidate without burning authorization. When the exact non-reduce-only request reaches the durable layer, authorization is persisted before any network call.

If persistence fails, the network is never called. If the same authorization already exists, it is recovery/reconciliation state and cannot be reused for resubmission.

If authorization persistence succeeds but v121 becomes HALTED before Gate 2, the raw network client is still not called. The newly persisted authorization remains burned as recovery-only evidence. This removes a dangerous interpretation where a pre-submit receipt could later be treated as permission to retry blindly.

Because PostgreSQL and an external HTTPS exchange call cannot participate in one atomic distributed transaction, there is an irreducible final TOCTOU interval after the last control read. Gate 1 plus Gate 2 minimize that interval and fail closed on every control loss that can be observed before network submission.

## Existing-trade protection remains available under HALT

The v121 guard is intentionally specific to `reduce_only=False` entry requests.

It does not disable:

- exchange position/execution reads;
- protection-state reads;
- stop / take-profit / trailing protection updates;
- required order cancellation/reconciliation;
- reduce-only close for the same trade identity;
- active checkpoint polling;
- fees/funding/account PnL reconciliation;
- terminal evidence handoff.

A kill switch that disabled those operations could strand exposure and would therefore be unsafe.

## End-to-end attribution

The durable identity chain remains:

```text
source snapshot + evidence/market rank
-> approval_id
-> deterministic entry orderLinkId
-> v120 immutable authorization
-> protected-entry provenance
-> terminal record
-> execution fees
-> funding
-> all-in net PnL / R multiple
```

v121 adds a separate operational audit chain for the short-lived permission window:

```text
operator + reason
-> sanitized connected-preflight canonical record
-> preflight SHA-256
-> ARM event_id
-> armed_until
```

Realized outcomes remain forbidden from automatically retuning exits, online selection thresholds or strategy promotion.

## Deployment / activation boundary

Code qualification alone is not connected-account evidence.

The minimum operational sequence before any future write-enabled Demo worker may be introduced is:

```text
1. bybit-demo-postgres-bootstrap -> VERIFIED_READY for v119-v121
2. bybit-demo-connected-preflight -> real authenticated read-only PASS
3. bybit-demo-control-plane status -> observe effective state
4. explicit ARM -> same invocation reruns connected read-only preflight
5. future operator-approved Demo worker -> consumes approval + ARM through canonical runtime
```

The current v121 control workflow contains no trading API credential and cannot submit an order. A future worker must use a **separate Demo trading credential**, never the read-only preflight key and never a mainnet key.

No schedule or autonomous ARM is allowed. Mainnet order creation/amendment/cancellation remains prohibited.
