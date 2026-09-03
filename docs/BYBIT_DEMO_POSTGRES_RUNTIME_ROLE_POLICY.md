# Bybit Demo PostgreSQL Runtime Role Policy — C2A1

Status: implementation candidate for issue #114 under parent security issue #107.

## Purpose

C2A0 made the v119 durable runtime lease canonical. C2A1 separates **migration/bootstrap authority** from the long-running runtime credential so a runtime process cannot bypass database safety through ownership, DDL, `TRUNCATE`, database-level object creation or role inheritance.

This is a PostgreSQL authority boundary only. It creates no exchange capability.

## Frozen schema

`migrations/v119/001_bybit_demo_durable_runtime.sql` remains unchanged.

C2A1 does **not** create v125 or another trading runtime version. It applies/validates role privileges around the existing v119 schema.

## Roles

### Bootstrap / migration role

May apply the frozen migration and reconcile object grants. It is operationally separate from the runtime credential.

C2A1 code does not create, rotate or store this credential.

### Runtime role

Must already exist and must be:

- LOGIN-capable;
- non-superuser;
- not CREATEDB;
- not CREATEROLE;
- not REPLICATION;
- not BYPASSRLS;
- not the bootstrap role;
- not owner of the current database/schema/v119 tables;
- not a member of any explicit PostgreSQL role.

The zero-membership rule is deliberately conservative. It prevents `SET ROLE`, inherited predefined broad roles, owner-role membership or another role grant from recreating authority that direct table ACLs appear to remove.

## Exact runtime privileges

Current database:

- `CREATE`: forbidden.

Schema `public`:

- `USAGE`: required;
- `CREATE`: forbidden.

`astra_bybit_demo_runtime_lease_v119`:

- `SELECT`: required;
- `INSERT`: required;
- `DELETE`: required;
- `UPDATE`: forbidden;
- `TRUNCATE`: forbidden;
- `REFERENCES`: forbidden;
- `TRIGGER`: forbidden.

`astra_bybit_demo_active_excursion_v119`:

- `SELECT`: required;
- `INSERT`: required;
- `UPDATE`: required;
- `DELETE`: required;
- `TRUNCATE`: forbidden;
- `REFERENCES`: forbidden;
- `TRIGGER`: forbidden.

The excursion privilege set comes from the audited historical CAS adapter: load/initialize/save/clear use exactly SELECT/INSERT/UPDATE/DELETE. The typed strategy-dependent excursion adapter is still not canonicalized by C2A1.

## Reconciliation behavior

`PostgresBybitDemoRuntimeRolePolicy.reconcile()`:

1. requires an existing safe runtime role;
2. refuses bootstrap/runtime identity collapse, global privilege flags, explicit role memberships and ownership;
3. revokes direct database CREATE, schema ACLs and table ACLs for that runtime role;
4. grants only schema USAGE and the exact v119 table DML above;
5. recomputes **effective** database/schema/table privileges using PostgreSQL `has_*_privilege` functions;
6. fails closed if PUBLIC/inherited authority still provides forbidden capability.

It never creates or alters a PostgreSQL role. If the environment grants broad privileges indirectly, reconciliation refuses readiness instead of trying to rewrite unrelated role topology.

## Runtime preflight

`PostgresBybitDemoRuntimeRolePreflight` is read-only and binds the **actual connected PostgreSQL user** to the expected runtime role.

Readiness is false on:

- connected-role mismatch;
- any explicit role membership;
- superuser/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS;
- database CREATE;
- schema CREATE;
- missing schema USAGE or required DML;
- effective TRUNCATE/REFERENCES/TRIGGER or other unexpected table privilege;
- runtime ownership of schema/table objects.

The evidence payload contains role names and boolean/privilege state only. It does not serialize a DSN or password.

## Production composition rule

The historical `PostgresBybitDemoRuntimeLease.migrate()` method remains for migration/test compatibility. A future canonical production composition must **not** call it with the runtime credential. Deployment/bootstrap applies schema and ACL policy; runtime startup performs the read-only role preflight before operational readiness can advance.

## Required qualification

Real PostgreSQL 16 tests must prove:

- exact lease DML works under the non-owner runtime credential;
- exact-owner lease fencing remains unchanged;
- the future excursion table accepts only the exact SELECT/INSERT/UPDATE/DELETE DML needed by the audited CAS contract;
- direct excess ACLs are removed deterministically;
- TRUNCATE rejected;
- ALTER TABLE rejected;
- DROP TABLE rejected;
- CREATE TABLE in `public` rejected;
- CREATE SCHEMA rejected;
- explicit group/role membership remains a blocker;
- superuser runtime roles are rejected;
- a different connected credential fails role-identity preflight;
- policy reconciliation is idempotent;
- full canonical security/regression gates remain green.

## Explicit non-capabilities

```text
automatic_role_creation_allowed = false
runtime_ddl_allowed = false
order_writes_supported = false
live_mainnet_order_routing_allowed = false
```

C2A1 does not prove broker connectivity, Demo execution, strategy edge, exact-head broker evidence, soak or production readiness.
