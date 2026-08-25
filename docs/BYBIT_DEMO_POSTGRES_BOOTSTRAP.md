# Bybit Demo PostgreSQL bootstrap

This gate operationalizes the complete durable Bybit Demo PostgreSQL schema before any connected runtime is allowed to depend on it.

It is database infrastructure only. It does not use a Bybit API key, does not create an order-capable client, and cannot submit, amend, cancel, protect, or close an order.

## Scope

The bootstrap applies these repository migrations, in order:

```text
migrations/v119/001_bybit_demo_durable_runtime.sql
migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql
migrations/v121/001_bybit_demo_control_plane.sql
migrations/v122/001_bybit_demo_postgres_session_risk.sql
migrations/v123/001_bybit_demo_session_start_audit.sql
```

v119 provides the canonical single-writer runtime lease and active excursion checkpoint. v120 provides immutable approval authorization, protected-entry provenance, and fully reconciled terminal evidence. v121 adds the append-only operator control journal used to HALT or short-lived ARM new Demo entries. v122 adds the restart-safe session-risk ledger: immutable opening equity, monotonic equity high-water, CAS revisions, and an append-only terminal all-in outcome journal. v123 adds immutable session-start provenance binding operator, reason, exact Git SHA, connected-preflight SHA, initial ledger revision and start timestamp.

The tool computes a SHA-256 fingerprint of each exact migration file and includes those fingerprints in the sanitized result artifact.

The external artifact schema remains `BYBIT_DEMO_POSTGRES_BOOTSTRAP_V2` for parser compatibility; `VERIFIED_READY` semantics now require the complete v119-v123 contract.

## Modes

### `verify`

Read-only. No DDL is executed. The command checks all required v119/v120 relations and triggers, v121 control relation/trigger, both v122 session-risk relations plus all four anti-reset triggers, and the v123 session-start audit relation plus both append-only/no-truncate triggers.

### `apply`

Schema mutation is permitted only when the stable exact confirmation phrase is supplied:

```text
APPLY_BYBIT_DEMO_DURABLE_SCHEMA
```

The phrase is intentionally schema-version neutral. Future durable migrations extend the verified migration set without forcing operators and runbooks to chase version-number confirmation strings.

The command acquires stable PostgreSQL session advisory lock `119999` before DDL so two bootstrap processes cannot apply the operational migration sequence concurrently. It then applies the complete migration set in order and independently re-opens read-only verifiers afterward.

If a migration fails inside its transaction, the failed transaction is rolled back before the advisory lock is released. A corrected run can safely re-run the idempotent migration sequence and must still pass final relation/trigger verification.

## GitHub Actions

Use the manual workflow:

```text
bybit-demo-postgres-bootstrap
```

Required GitHub Secret:

```text
BYBIT_DEMO_DATABASE_DSN
```

There is no schedule. The operational workflow supports `verify` and `apply`; apply additionally requires the exact confirmation phrase above.

The pull-request job never uses the operational DSN. It qualifies the complete v119-v123 lifecycle and v122 risk durability against an isolated PostgreSQL 16 service database. The dedicated session-start workflow separately qualifies v123 provenance creation and immutability because append-only session state must not be cross-contaminated between tests.

## Sanitized artifact

The workflow writes:

```text
artifacts/bybit-demo-postgres-bootstrap.json
```

The artifact contains status, whether schema mutation occurred, relation/trigger readiness, and migration paths with SHA-256 fingerprints. It deliberately excludes DSN, host, database name, credentials, balances, positions, prices, quantities and order identities.

## Required deployment sequence

```text
1. Configure BYBIT_DEMO_DATABASE_DSN.
2. Run bybit-demo-postgres-bootstrap in verify mode.
3. If SCHEMA_NOT_READY, verify target identity and backup/PITR posture.
4. Run apply with APPLY_BYBIT_DEMO_DURABLE_SCHEMA.
5. Run verify again and require VERIFIED_READY for v119-v123.
6. Configure separate BYBIT_DEMO_READONLY_API_KEY / SECRET.
7. Run bybit-demo-connected-preflight and inspect its sanitized evidence.
8. Use bybit-demo-control-plane status; absence of any control event is HALTED by default.
9. Run bybit-demo-session-start status.
10. If NOT_INITIALIZED, initialize v122 + v123 atomically through the explicit flat/HALTED gate.
11. Re-run session status and require INITIALIZED with verified provenance.
12. ARM only through bybit-demo-control-plane. ARM reruns connected read-only preflight and only accepts READY_FOR_MANUAL_OPERATOR_APPROVAL.
13. Only a separately protected, operator-approved Demo execution runtime may consume that short-lived ARM state.
```

A successful bootstrap proves database schema readiness only. It does not prove connected Bybit account readiness, does not initialize a risk session, and does not authorize trading.

## Safety boundary

Every bootstrap result remains:

```text
database_identity_exposed=false
bybit_credentials_required=false
bybit_order_writes_supported=false
live_mainnet_order_routing_allowed=false
```

Mainnet remains unchanged and read-only.
