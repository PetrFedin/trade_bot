# Bybit Demo durable audit lifecycle (v120)

## Purpose

The canonical Demo runtime now has PostgreSQL-backed single-writer lease and active-trade checkpoint
state. The remaining audit chain must also survive worker restarts and replica changes without
changing record identity:

```text
evidence-ranked approval
-> pre-submit authorization
-> protected-entry provenance
-> fully reconciled terminal evidence
```

v120 provides PostgreSQL stores for those three already-qualified lifecycle records. It does not
create a new decision, selector, risk policy or order path.

## Identity contract

The PostgreSQL stores deliberately reuse the existing qualified file-store codecs.

For the same logical record:

```text
file backend canonical record
== PostgreSQL backend canonical_record

SHA256(file canonical record)
== PostgreSQL record_sha256
```

This byte-stable identity is important. Moving from local files to PostgreSQL must not create a new
audit identity for the same approval, entry provenance or terminal outcome.

CI compares file-store and PostgreSQL receipt SHA values for each layer.

## Approval authorization

`PostgresBybitDemoApprovedEntryAuthorizationStore` persists the exact pre-network authorization that
is burned immediately before the raw Demo entry mutation.

The database row keeps:

- entry `orderLinkId`;
- `approval_id`;
- source v111 snapshot ID;
- source evidence rank;
- source market rank;
- canonical outcome-free record;
- canonical SHA-256.

Database constraints require:

```text
outcome_free=true
order_submission_supported=false
realized_pnl_storage_allowed=false
live_mainnet_order_routing_allowed=false
```

The store has no order-submit method. A second identical write is idempotent. The same entry
identity with a different approval/source payload is a conflict and cannot replace history.

## Protected-entry provenance

`PostgresBybitDemoEntryProvenanceStore` persists the existing immutable protected-entry decision and
execution provenance.

It remains outcome-free. In particular it cannot contain or authorize use of future realized PnL
for selection. Its safety contract remains:

```text
immutable_records=true
realized_pnl_storage_allowed=false
order_writes_supported=false
live_mainnet_order_routing_allowed=false
```

The store supports typed load/round-trip so a restarted worker or later attribution process can
recover exactly the same provenance object from PostgreSQL.

## Terminal evidence

`PostgresBybitDemoTerminalEvidenceStore` accepts only the already-qualified fully reconciled terminal
evidence object. It does not recompute PnL itself.

Before persistence, the existing terminal codec/validator still requires a complete all-in result,
including the accounting/funding reconciliation already enforced by the terminal evidence layer.

The database row requires:

```text
fully_reconciled_all_in=true
diagnostics_only=true
exit_threshold_retuning_allowed=false
live_mainnet_order_routing_allowed=false
```

The same entry `orderLinkId` cannot later be replaced by another checkpoint revision or changed
all-in outcome.

## Append-only database boundary

The three v120 tables are historical/audit records, not mutable runtime state:

```text
astra_bybit_demo_approved_entry_authorization_v120
astra_bybit_demo_entry_provenance_v120
astra_bybit_demo_terminal_evidence_v120
```

UPDATE and DELETE are rejected by a database trigger. `PUBLIC` privileges are revoked.

This differs intentionally from the v119 runtime lease and active excursion tables. v119 contains
operational singleton state that must change under controlled release/CAS. v120 contains completed
or pre-submit audit facts that must never be rewritten.

## Two-phase terminal handoff

The existing terminal handoff protocol remains unchanged:

```text
1. persist immutable fully reconciled terminal evidence
2. verify terminal receipt identity / checksum
3. clear the exact active checkpoint by expected CAS revision
```

The PostgreSQL qualification test executes this real handoff using:

```text
PostgresBybitDemoTerminalEvidenceStore
+
PostgresBybitDemoExcursionStore
```

A failure to persist terminal evidence prevents checkpoint clear. A stale checkpoint revision cannot
clear newer active-trade state.

This preserves restart safety around the terminal boundary: a worker may repeat an idempotent final
evidence write after restart, but it cannot silently overwrite the terminal result or clear the
wrong checkpoint.

## Injection into the canonical approved runtime

A persistent connected Demo worker can use PostgreSQL-backed lifecycle stores without changing the
qualified execution logic:

```python
approval_authorization_store = PostgresBybitDemoApprovedEntryAuthorizationStore(database_dsn)
entry_provenance_store = PostgresBybitDemoEntryProvenanceStore(database_dsn)
terminal_evidence_store = PostgresBybitDemoTerminalEvidenceStore(database_dsn)
```

Together with v119:

```python
runtime_lease = PostgresBybitDemoRuntimeLease(database_dsn)
excursion_store = PostgresBybitDemoExcursionStore(database_dsn)
```

these remove the core local-filesystem dependencies from the approval/managed-trade audit chain.

## What v120 does not enable

v120 does not enable connected Demo trading by itself.

Before any connected Demo worker is activated, the remaining deployment and accounting state must
be audited for restart durability, including:

- session-risk ledger and daily/session limits;
- previous-trade/accounting reconciliation state;
- worker liveness and operator recovery procedure;
- protected Demo credentials and secret rotation;
- durable deployment hosting rather than an ephemeral Actions job for managed positions;
- startup reconciliation between PostgreSQL state and actual Bybit Demo positions/orders.

Only after those boundaries are proven should a connected Demo worker be allowed to consume a
short-lived operator approval.

Mainnet remains a separate GET-only/read-only account-context boundary. Nothing in v120 adds or
prepares a mainnet order mutation surface.
