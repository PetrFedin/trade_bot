# Bybit Demo PostgreSQL bootstrap

This gate operationalizes the durable Bybit Demo PostgreSQL schema before any connected runtime is allowed to depend on it.

It is database infrastructure only. It does not use a Bybit API key, does not create an order-capable client, and cannot submit, amend, cancel, protect, or close an order.

## Scope

The bootstrap applies exactly these repository migrations, in order:

```text
migrations/v119/001_bybit_demo_durable_runtime.sql
migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql
migrations/v121/001_bybit_demo_control_plane.sql
migrations/v122/001_bybit_demo_postgres_session_risk.sql
migrations/v123/001_bybit_demo_runtime_lease_recovery.sql
```

- **v119** — canonical single-writer runtime lease and active excursion checkpoint.
- **v120** — immutable approval authorization, protected-entry provenance, and fully reconciled terminal evidence.
- **v121** — append-only operator HALT / short-lived ARM control journal for new Demo exposure.
- **v122** — restart-safe session-risk ledger with immutable opening equity, monotonic equity high-water, CAS revisions, and append-only terminal all-in outcomes.
- **v123** — append-only controlled orphan-lease recovery audit. It records an exact lease-owner fingerprint and external process-stop evidence before the same transaction deletes that exact v119 lease. UPDATE/DELETE and TRUNCATE are both physically rejected.

The tool computes a SHA-256 fingerprint of each exact migration file and includes those fingerprints in the sanitized result artifact.

## Modes

### `verify`

Read-only. No DDL is executed. The command checks all required v119/v120 relations and triggers, the v121 control relation/trigger, both v122 session-risk relations and all four v122 anti-reset/append-only triggers, plus the v123 recovery relation and both v123 append-only/no-truncate triggers.

### `apply`

Schema mutation is permitted only when the exact confirmation phrase is supplied:

```text
APPLY_BYBIT_DEMO_V119_V123
```

The command acquires PostgreSQL session advisory lock `119123` before DDL so two bootstrap processes cannot apply the operational migration sequence concurrently. It then applies v119 through v123 in order and independently re-opens read-only verifiers afterward.

The old `APPLY_BYBIT_DEMO_V119_V122` phrase is intentionally rejected: it did not express operator intent to apply the new v123 DDL.

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

The pull-request qualification never uses the production DSN. It applies and verifies the migration stack against PostgreSQL 16.

## Sanitized artifact

The workflow writes:

```text
artifacts/bybit-demo-postgres-bootstrap.json
```

The artifact contains status, whether schema mutation occurred, relation/trigger readiness, and migration paths with SHA-256 fingerprints. It excludes DSN, host, database name, credentials, balances, positions, prices, quantities, raw lease owner tokens, and order identities.

## Required deployment sequence

```text
1. Configure BYBIT_DEMO_DATABASE_DSN.
2. Run bybit-demo-postgres-bootstrap in verify mode.
3. If SCHEMA_NOT_READY, verify target identity and backup/PITR posture.
4. Run apply with APPLY_BYBIT_DEMO_V119_V123.
5. Run verify again and require VERIFIED_READY.
6. Configure separate BYBIT_DEMO_READONLY_API_KEY / SECRET.
7. Run bybit-demo-connected-preflight and inspect its sanitized evidence.
8. Use bybit-demo-control-plane status; absence of any event remains HALTED by default.
9. Initialize the v122 risk singleton exactly once through bybit-demo-session-start while flat/HALTED.
10. New exposure may be ARM-ed only from a fresh connected preflight and still requires a separate operator approval plus immutable authorization.
11. Persistent supervisor management may service an existing checkpoint but has no entry authority.
12. If a hard-killed process leaves an orphan v119 lease, use the separate controlled v123 recovery runbook. Never delete the lease manually and never use age/TTL takeover.
```

A successful bootstrap proves database schema readiness only. It does not prove connected Bybit account readiness, does not initialize a risk session, does not recover a lease, and does not authorize trading.

## Safety boundary

Every bootstrap result remains:

```text
database_identity_exposed=false
bybit_credentials_required=false
bybit_order_writes_supported=false
live_mainnet_order_routing_allowed=false
```

Mainnet remains unchanged and read-only.
