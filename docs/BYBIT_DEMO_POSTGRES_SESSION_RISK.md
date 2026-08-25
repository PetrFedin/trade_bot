# Bybit Demo PostgreSQL session-risk ledger v122

v122 removes the remaining local-process risk-state dependency before a persistent connected Demo worker is allowed.

## Why this is required

A worker restart must not reset risk memory. The following state must survive runner, process, or container restarts:

- session opening equity;
- equity high-water mark;
- fully reconciled all-in terminal PnL;
- execution fees used by the session risk model;
- consecutive-loss history derived from terminal outcomes;
- the exact CAS revision of the durable ledger.

Recreating an empty ledger after a restart would weaken loss, drawdown, and cost guards and is therefore prohibited.

## Storage contract

`astra_bybit_demo_session_risk_v122` stores one active lifetime risk checkpoint. Its opening equity and safety identity are immutable. Its high-water mark can only increase. Updates require the exact prior SHA-256 revision.

`astra_bybit_demo_session_trade_outcome_v122` is an append-only journal keyed by the canonical Demo entry `orderLinkId`. Each row stores a canonical record plus SHA-256 and only fully reconciled terminal economics.

Every load verifies the active checkpoint against the append-only journal. A disagreement fails closed.

## Anti-reset behavior

The database rejects:

- UPDATE or DELETE of a terminal outcome;
- TRUNCATE of the outcome journal;
- DELETE or TRUNCATE of the active risk ledger;
- changing opening equity or safety markers;
- decreasing the stored equity high-water;
- decreasing outcome count;
- modifying or removing an outcome already present in the canonical checkpoint.

The application store additionally rejects stale CAS revisions, conflicting economics for an existing entry identity, and initialization with imported historical outcomes.

There is deliberately no `reset`, `clear`, `truncate`, or automatic stale-session takeover method.

## Initialization boundary

Schema bootstrap does **not** initialize the risk ledger. Initialization is a separate future operational gate and must require at minimum:

```text
exchange flat
no pending orders
v119-v122 VERIFIED_READY
v121 HALTED
no active runtime lease/checkpoint
connected read-only account evidence fresh
explicit operator action
```

Opening equity must be taken from an authenticated Demo wallet read in that same bounded operation. Once initialized, subsequent process starts load the durable ledger; they do not create a new one.

## Runtime handoff

A persistent Demo runtime should follow this order:

```text
load v122 checkpoint
→ read current Demo wallet
→ advance/persist high-water by CAS
→ reconcile any prior terminal trade
→ append fully reconciled outcome by CAS
→ derive current session risk state
→ only then evaluate whether a new entry is allowed
```

If any persistence/reconciliation/CAS step fails, new exposure stays blocked.

## Safety boundary

v122 is not an execution surface. The store exposes no Bybit client and no order mutation capability:

```text
order_writes_supported=false
live_mainnet_order_routing_allowed=false
automatic_reset_allowed=false
immutable_trade_outcomes=true
```

Mainnet remains read-only. v122 is a prerequisite for a future operator-gated Demo worker, not authorization to create one or to trade.
