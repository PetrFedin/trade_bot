# Bybit Demo PostgreSQL bootstrap

This gate operationalizes the durable Bybit Demo PostgreSQL schema before any connected runtime is allowed to depend on it.

It is database infrastructure only. It does not use a Bybit API key, does not create an order-capable client, and cannot submit, amend, cancel, protect, or close an order.

## Scope

The bootstrap applies exactly these repository migrations, in order:

```text
migrations/v119/001_bybit_demo_durable_runtime.sql
migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql
migrations/v121/001_bybit_demo_control_plane.sql
```

v119 provides the canonical single-writer runtime lease and active excursion checkpoint. v120 provides immutable approval authorization, protected-entry provenance, and fully reconciled terminal evidence. v121 adds the append-only operator control journal used to HALT or short-lived ARM new Demo entries.

The tool computes a SHA-256 fingerprint of each exact migration file and includes those fingerprints in the sanitized result artifact.

## Modes

### `verify`

Read-only. No DDL is executed. The command checks all required v119/v120 relations and triggers plus the v121 control relation and append-only trigger.

### `apply`

Schema mutation is permitted only when the exact confirmation phrase is supplied:

```text
APPLY_BYBIT_DEMO_V119_V121
```

The command acquires PostgreSQL session advisory lock `119121` before DDL so two bootstrap processes cannot apply the same operational migration sequence concurrently. It then applies v119, v120, and v121 in order and independently re-opens read-only verifiers afterward.

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

The pull-request job never uses the operational DSN. It qualifies the full v119-v121 lifecycle against an isolated PostgreSQL 16 service database.

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
4. Run apply with APPLY_BYBIT_DEMO_V119_V121.
5. Run verify again and require VERIFIED_READY.
6. Configure separate BYBIT_DEMO_READONLY_API_KEY / SECRET.
7. Run bybit-demo-connected-preflight and inspect its sanitized evidence.
8. Use bybit-demo-control-plane status; absence of any control event is HALTED by default.
9. ARM only through bybit-demo-control-plane. ARM itself reruns connected read-only preflight and only accepts READY_FOR_MANUAL_OPERATOR_APPROVAL.
10. Only a separately protected, operator-approved Demo execution runtime may consume that short-lived ARM state.
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
