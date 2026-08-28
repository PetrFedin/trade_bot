# Bybit Demo logical database identity v124

v124 closes a remaining provenance gap in the protected Demo operational chain: two independently initialized PostgreSQL databases can share the same configured hostname, port and database name over time, especially after DNS changes or environment reconfiguration.

The operational trust-zone sidecar therefore no longer relies only on DSN endpoint semantics. It also binds an immutable logical database identity created once inside the database itself.

## v124 singleton

Migration:

```text
migrations/v124/001_bybit_demo_operational_database_identity.sql
```

creates exactly one row in:

```text
astra_bybit_demo_operational_identity_v124
```

with fixed identity name:

```text
CANONICAL_DEMO_OPERATIONAL_DATABASE
```

and a PostgreSQL core `gen_random_uuid()` value generated only when the singleton is first created.

The table permits no second logical identity because `identity_name` is the primary key and is constrained to the canonical constant.

## Immutability

The logical identity is infrastructure metadata only. It cannot submit or manage an order and carries explicit safety markers:

```text
immutable_record = true
diagnostics_only = true
order_writes_supported = false
live_mainnet_order_routing_allowed = false
```

v124 installs triggers that reject:

- UPDATE;
- DELETE;
- TRUNCATE.

The migration is idempotent. Re-running bootstrap uses `ON CONFLICT DO NOTHING`, so the existing UUID is preserved.

## Backup, restore and failover semantics

The UUID is a **logical database lineage identity**, not a physical PostgreSQL cluster identifier.

Expected behavior:

- password/user rotation on the same database: identity preserved;
- connection-pool or DNS endpoint change with the same restored logical database: identity preserved;
- normal backup/restore used as continuity of the same operational database: identity preserved;
- independently bootstrapped database: new UUID;
- accidental switch to another independently initialized database at the same DSN endpoint: detected by trust-zone HMAC drift.

A copied backup deliberately retains the logical UUID. If a clone is intentionally promoted as the continuation of the same operational database, that is the desired behavior. Physical-cluster identity is intentionally not required because doing so would introduce stronger PostgreSQL monitoring privileges and would make legitimate restore/failover continuity brittle.

## Read-only reader

`PostgresBybitDemoOperationalDatabaseIdentityReader`:

- runs inside a read-only transaction;
- verifies the v124 relation exists;
- verifies both immutability triggers exist;
- requires exactly one row;
- requires a canonical UUID;
- requires all safety markers;
- exposes no schema or order mutation surface.

The raw UUID is used only inside the protected process. It is not included in bootstrap, readiness, zone or release artifacts.

## Bootstrap v4

`verify_bybit_demo_postgres_schema()` now requires v119 through v124 and returns:

```text
schema = BYBIT_DEMO_POSTGRES_BOOTSTRAP_V4
logical_database_identity_verified = true
```

before `VERIFIED_READY` is possible.

The preferred explicit apply phrase is:

```text
APPLY_BYBIT_DEMO_V119_V124
```

The former `APPLY_BYBIT_DEMO_V119_V123` phrase remains a temporary backward-compatible alias for the already-explicit `apply` operation so existing protected runbooks do not fail during rollout. Both phrases apply and verify the complete v119-v124 stack. Any unrelated phrase is rejected.

## Trust-zone HMAC v2

`BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V2` computes its database token over both:

1. canonical DSN resource semantics (host/hostaddr/port/database/SSL target settings), and
2. the v124 logical UUID.

The UUID is inside the HMAC input and never serialized.

This gives the intended behavior:

```text
same endpoint + same logical UUID -> same DB binding
same endpoint + different logical UUID -> different DB binding
same logical DB + credential rotation -> same DB binding
```

Every v2 sidecar records only:

```text
logical_database_identity_verified = true
```

plus the opaque HMAC token.

## Activation and release gates

Activation readiness cannot become ready from a legacy bootstrap V3 artifact or from V4 evidence where logical identity is not verified.

The final operational release assembler requires every supplied zone sidecar to be schema V2 and to carry:

```text
logical_database_identity_verified = true
```

before delegating to the existing exact-head / same-account / same-zone / ARM-entry-HALT-recovery lineage gates.

## Safety boundary

v124 adds no:

- strategy or signal behavior;
- risk or sizing behavior;
- order or protection mutation;
- ARM/HALT automation;
- recovery automation;
- mainnet routing;
- PostgreSQL monitoring role such as `pg_monitor`.

Pull-request qualification uses isolated PostgreSQL 16 only. No protected workflow dispatch and no Demo order are part of v124 qualification.
