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
migrations/v124/001_bybit_demo_operational_database_identity.sql
```

- **v119** — canonical single-writer runtime lease and active excursion checkpoint.
- **v120** — immutable approval authorization, protected-entry provenance, and fully reconciled terminal evidence.
- **v121** — append-only operator HALT / short-lived ARM control journal for new Demo exposure.
- **v122** — restart-safe session-risk ledger with immutable opening equity, monotonic equity high-water, CAS revisions, and append-only terminal all-in outcomes.
- **v123** — append-only controlled orphan-lease recovery audit with exact lease-owner fingerprint and external process-stop evidence.
- **v124** — one immutable logical operational-database UUID used only inside protected trust-zone HMAC binding. UPDATE/DELETE/TRUNCATE are physically rejected and the raw UUID is never exported.

The tool computes a SHA-256 fingerprint of each exact migration file and includes those fingerprints in the sanitized result artifact.

## Modes

### `verify`

Read-only. No DDL is executed. `VERIFIED_READY` now requires the complete v119-v124 contract, including:

- v119/v120 durable runtime and audit relations/triggers;
- v121 control relation and append-only protections;
- both v122 session-risk relations and all four anti-reset/append-only triggers;
- v123 recovery relation and append-only/no-truncate triggers;
- v124 identity relation, both immutability triggers, exactly one valid canonical UUID row, and all no-order/no-mainnet safety markers.

The result schema is:

```text
BYBIT_DEMO_POSTGRES_BOOTSTRAP_V4
```

and ready evidence includes:

```text
logical_database_identity_verified = true
database_identity_exposed = false
```

### `apply`

Schema mutation remains an explicit operator action. The preferred confirmation phrase is:

```text
APPLY_BYBIT_DEMO_V119_V124
```

During rollout, the former explicit phrase:

```text
APPLY_BYBIT_DEMO_V119_V123
```

remains a deprecated compatibility alias. Both phrases still require `mode=apply` and apply/verify the complete v119-v124 stack. Any unrelated phrase is rejected.

The command acquires PostgreSQL session advisory lock `119124` before DDL so two bootstrap processes cannot apply the operational migration sequence concurrently. It then applies v119 through v124 in order and independently re-opens read-only verifiers afterward.

If a migration fails inside its transaction, the failed transaction is rolled back before the advisory lock is released. A corrected run can safely re-run the idempotent migration sequence and must still pass final relation/trigger/logical-identity verification.

## v124 identity semantics

The v124 UUID identifies the logical operational database lineage, not the physical PostgreSQL cluster.

It is generated with PostgreSQL core `gen_random_uuid()` only when the singleton is first created and is preserved by idempotent bootstrap and normal backup/restore. This deliberately avoids requiring `pg_monitor` or other new monitoring privileges.

See `BYBIT_DEMO_LOGICAL_DATABASE_IDENTITY_V124.md` for the full contract.

## GitHub Actions

Use the manual workflow:

```text
bybit-demo-postgres-bootstrap
```

Required GitHub Secret:

```text
BYBIT_DEMO_DATABASE_DSN
```

There is no schedule. The operational workflow supports `verify` and `apply`; apply additionally requires one of the explicit confirmation phrases above.

The pull-request qualification never uses the production DSN. It applies and verifies the migration stack against isolated PostgreSQL 16.

## Sanitized artifact

The workflow writes:

```text
artifacts/bybit-demo-postgres-bootstrap.json
```

The artifact contains status, whether schema mutation occurred, relation/trigger readiness, logical-identity verification status, and migration paths with SHA-256 fingerprints.

It excludes DSN, host, database name, database UUID, credentials, balances, positions, prices, quantities, raw lease owner tokens, and order identities.

## Required deployment sequence

```text
1. Configure BYBIT_DEMO_DATABASE_DSN and confirm backup/PITR posture.
2. Run bybit-demo-postgres-bootstrap in verify mode.
3. If SCHEMA_NOT_READY, verify the intended target and migration plan.
4. Run apply with preferred phrase APPLY_BYBIT_DEMO_V119_V124.
5. Run verify again and require VERIFIED_READY + logical_database_identity_verified=true.
6. Configure/retain the separate BYBIT_DEMO_ZONE_BINDING_SECRET in the protected bybit-demo environment.
7. Run activation readiness; its zone sidecar must bind the v124 logical identity without exposing it.
8. Initialize/resume v122 session state through the existing explicit session-start flow.
9. New exposure may be ARM-ed only from a fresh connected preflight and still requires separate operator approval plus immutable authorization.
10. Persistent supervisor management may service an existing checkpoint but has no entry authority.
11. If a hard-killed process leaves an orphan v119 lease, use the controlled v123 recovery runbook; never delete the lease manually and never use age/TTL takeover.
12. Assemble release evidence only from exact-head V2 zone sidecars that all prove the same v124-backed operational database binding.
```

A successful bootstrap proves database schema/readiness only. It does not prove connected Bybit account readiness, does not initialize a risk session, does not recover a lease, and does not authorize trading.

## Safety boundary

Every bootstrap result remains:

```text
database_identity_exposed=false
bybit_credentials_required=false
bybit_order_writes_supported=false
live_mainnet_order_routing_allowed=false
```

Mainnet remains unchanged and read-only.
