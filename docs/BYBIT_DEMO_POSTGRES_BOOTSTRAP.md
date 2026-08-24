# Bybit Demo PostgreSQL bootstrap

This gate operationalizes the existing v119/v120 durable Demo schema before any connected Bybit Demo runtime is allowed to depend on it.

It is database infrastructure only. It does not use a Bybit API key, does not create an order-capable client, and cannot submit, amend, cancel, protect, or close an order.

## Scope

The bootstrap applies exactly these repository migrations, in order:

```text
migrations/v119/001_bybit_demo_durable_runtime.sql
migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql
```

v119 provides the canonical single-writer runtime lease and active excursion checkpoint. v120 provides immutable approval authorization, protected-entry provenance, and fully reconciled terminal evidence with database-level append-only guards.

The tool computes a SHA-256 fingerprint of each exact migration file and includes those fingerprints in the sanitized result artifact.

## Modes

### `verify`

Read-only. No DDL is executed. The command checks that all required v119/v120 relations exist and that all three v120 append-only triggers are present.

### `apply`

Schema mutation is permitted only when the exact confirmation phrase is supplied:

```text
APPLY_BYBIT_DEMO_V119_V120
```

The command acquires a PostgreSQL session advisory lock before DDL so two bootstrap processes cannot apply the same operational migration sequence concurrently. It then applies v119 followed by v120 and independently re-opens the read-only operational-state verifier.

If a migration fails inside its transaction, the failed transaction is rolled back before the advisory lock is released. A later corrected run can safely re-run the idempotent migration sequence and must still pass the final relation/trigger verification.

## GitHub Actions

Use the manual workflow:

```text
bybit-demo-postgres-bootstrap
```

Required GitHub Secret:

```text
BYBIT_DEMO_DATABASE_DSN
```

There is no schedule. The operational workflow supports `verify` and `apply`. `apply` additionally requires the exact confirmation phrase above.

The pull-request job never uses the production DSN. It qualifies the lifecycle against an isolated PostgreSQL 16 service database.

## Sanitized artifact

The workflow writes:

```text
artifacts/bybit-demo-postgres-bootstrap.json
```

The artifact includes:

- status;
- whether schema mutation occurred;
- required-relation readiness;
- append-only-trigger readiness;
- v119/v120 migration paths and SHA-256 fingerprints.

It deliberately excludes:

- DSN;
- host;
- database name;
- username/password;
- Bybit credentials;
- account balances;
- positions;
- order identities.

## Required deployment sequence

The safe operational sequence is:

```text
1. Configure BYBIT_DEMO_DATABASE_DSN
2. Run bybit-demo-postgres-bootstrap in verify mode
3. If SCHEMA_NOT_READY, inspect the target and backup/PITR posture
4. Run apply with APPLY_BYBIT_DEMO_V119_V120
5. Run verify again and require VERIFIED_READY
6. Configure separate BYBIT_DEMO_READONLY_API_KEY / SECRET
7. Run bybit-demo-connected-preflight
8. Require READY_FOR_MANUAL_OPERATOR_APPROVAL or a fully reconciled EXISTING_TRADE_MANAGEMENT_REQUIRED state
9. Only after that consider a separate write-enabled, operator-approved Demo worker
```

A successful bootstrap proves database schema readiness only. It does not prove connected Bybit account readiness and does not authorize trading.

## Safety boundary

Every bootstrap result remains:

```text
database_identity_exposed=false
bybit_credentials_required=false
bybit_order_writes_supported=false
live_mainnet_order_routing_allowed=false
```

Mainnet remains unchanged and read-only.
