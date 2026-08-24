# Bybit Demo durable runtime state (v119)

## Purpose

A connected managed-trade runtime cannot safely rely on the local filesystem of an ephemeral
GitHub-hosted runner. Two state objects are safety-critical:

1. the canonical single-writer runtime lease;
2. the active trade excursion checkpoint.

Losing the lease can allow two processes to cross the new-entry boundary. Losing the active
checkpoint can make a restarted process forget an already-open managed position and its accumulated
MFE/MAE / partial-close state.

v119 provides PostgreSQL implementations of the same interfaces before any connected Demo write
workflow is enabled.

## PostgreSQL runtime lease

`PostgresBybitDemoRuntimeLease` preserves the conservative semantics of
`JsonFileBybitDemoRuntimeLease`:

- exactly one singleton lease named `CANONICAL_DEMO_TRADING_RUNTIME`;
- cryptographically random 32-byte owner token;
- process ID and acquisition time retained for diagnosis;
- a second acquire fails closed with `FileExistsError`;
- release requires the exact owner token;
- no TTL;
- no automatic stale takeover;
- no mainnet-routing capability.

The absence of automatic stale takeover is deliberate. A crashed process and a merely slow process
are indistinguishable without an external liveness proof. Automatically stealing an old lease could
therefore create duplicate exposure. An orphaned lease must block new entries until an operator has
independently verified the prior runtime and exchange state.

## PostgreSQL active excursion checkpoint

`PostgresBybitDemoExcursionStore` implements the existing `BybitDemoExcursionStore` contract using
one singleton `ACTIVE` row.

It preserves:

- exact entry `orderLinkId` identity;
- the complete excursion state used by the managed-trade lifecycle;
- the existing canonical SHA-256 revision contract;
- initialize-only-when-empty semantics;
- compare-and-swap save by `entry_order_link_id + expected_revision`;
- compare-and-swap clear by `expected_revision`;
- explicit `FileNotFoundError` when no active checkpoint exists;
- diagnostics-only / no-exit-retuning / no-mainnet constraints.

For the same state and order identity, the PostgreSQL backend must produce the same revision as the
existing JSON-file backend. CI verifies that invariant so changing storage backends does not change
checkpoint identity.

## Schema

Migration:

```text
migrations/v119/001_bybit_demo_durable_runtime.sql
```

Tables:

```text
astra_bybit_demo_runtime_lease_v119
astra_bybit_demo_active_excursion_v119
```

These are mutable operational singleton tables, not historical evidence ledgers. The lease must be
deleted on correct release; the active checkpoint must evolve under CAS and be deleted after a
terminal handoff. Historical evidence, entry provenance and post-trade attribution remain separate
immutable/audited layers.

`PUBLIC` privileges are revoked for both tables.

## Concurrency contract

### Lease

```text
INSERT singleton
-> success: this runtime owns new-entry boundary
-> conflict: validate existing row and fail closed
```

There is no age-based conflict resolution.

### Active checkpoint

```text
initialize: INSERT singleton only when absent
save:       UPDATE ... WHERE entry_order_link_id=? AND revision=?
clear:      DELETE ... WHERE revision=?
```

A stale writer cannot overwrite or clear a newer checkpoint. If state disappeared unexpectedly,
that is reported as a runtime error rather than silently recreated.

## Deployment use

A persistent connected Demo worker should instantiate:

```python
runtime_lease = PostgresBybitDemoRuntimeLease(database_dsn)
excursion_store = PostgresBybitDemoExcursionStore(database_dsn)
```

and inject them into the already-qualified canonical operator-approved runtime.

The same PostgreSQL service should be reachable across worker restarts and replicas. The database
migration must be applied through the controlled migration path before the worker is permitted to
enter write-enabled Demo mode.

## What still blocks connected Demo activation

v119 removes the two state-loss paths that could directly cause duplicate exposure or forgotten
active trades. It does **not** by itself enable a connected Demo execution job.

Before activation, the remaining local audit/lifecycle stores must be reviewed and, where needed,
moved to a deployment-durable backend, including the operator approval authorization lineage,
protected-entry provenance and terminal evidence/attribution handoff. The connected worker also
needs a durable deployment model, protected Demo credentials and an operational recovery procedure.

Mainnet remains read-only throughout this work.
