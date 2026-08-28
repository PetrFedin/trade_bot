# Bybit Demo activation readiness

This checkpoint turns the separate Demo infrastructure gates into one operator-facing readiness verdict before any explicit ARM or exact trade approval is used.

It remains non-trading. The workflow does not ARM v121, does not create an approval and does not call any order mutation endpoint.

## Protected workflow

Manual workflow:

```text
bybit-demo-activation-readiness
```

Operational execution uses:

```text
runs-on: [self-hosted, bybit-demo]
environment: bybit-demo
```

There is no schedule.

## Evidence collected in one invocation

The workflow performs, in order:

```text
1. PostgreSQL v119-v124 verify only
2. fixed-egress connected read-only Demo preflight
3. GET-only dedicated Demo trading credential preflight
4. GET-only proof that read-only and trading credentials belong to one exact Demo account
5. v121 control status read
6. fail-closed v124 manifest assembly
7. v124-backed operational-zone sidecar generation
```

The PostgreSQL artifact is `BYBIT_DEMO_POSTGRES_BOOTSTRAP_V4`. `VERIFIED_READY` requires the complete v119-v124 contract, including the immutable logical database identity singleton.

The readiness wrapper rejects legacy V3 bootstrap evidence and requires:

```text
logical_database_identity_verified = true
```

before ARM readiness can be reported.

The source steps write sanitized evidence artifacts. The final assembler reads those files, validates schemas and safety flags, hashes exact bytes with SHA-256, binds the manifest to the checked-out Git commit, and emits one readiness artifact.

## Logical operational database proof

v124 adds an immutable logical database UUID stored only inside PostgreSQL. It is not exported in readiness evidence.

After readiness assembly, the protected zone sidecar reads and validates that identity and includes it only inside the database HMAC input. The sidecar reports:

```text
schema = BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V2
logical_database_identity_verified = true
```

This distinguishes an independently bootstrapped database even if it later appears at the same configured DSN endpoint.

See `BYBIT_DEMO_LOGICAL_DATABASE_IDENTITY_V124.md` and `BYBIT_DEMO_OPERATIONAL_ZONE_BINDING.md`.

## Same-account proof

Bybit `GET /v5/user/query-api` identifies the account that owns an API key. The readiness workflow performs two authenticated GET-only identity reads and requires exact equality of:

```text
userID
parentUid
isMaster
```

The raw values are compared only in memory and are never serialized. The sanitized artifact contains only booleans such as `same_user_id`, `same_parent_uid`, `same_master_scope` and final status `VERIFIED_SAME_ACCOUNT` or `BLOCKED`.

This closes a configuration failure mode where accounting/preflight could read Demo account A while the write-capable key belonged to Demo account B.

The same guard is repeated by operator-approved entry and persistent supervisor before either constructs its order-capable client.

## Step-scoped secrets

Credentials are not placed in a job-level environment.

- PostgreSQL verify receives only `BYBIT_DEMO_DATABASE_DSN`.
- Connected preflight receives the DSN plus the Demo read-only key/secret.
- Trading credential preflight receives the Demo trading key/secret plus read-only/mainnet namespace fingerprints.
- Same-account identity proof receives the Demo read-only and trading key/secret pairs, but no database DSN and no order-capable client.
- Control status receives only the DSN.
- Manifest assembly receives no credential secret.
- Zone sidecar receives the DSN, Demo read-only credential and separate `BYBIT_DEMO_ZONE_BINDING_SECRET`; it performs PostgreSQL reads plus authenticated GET only.

The GitHub-hosted final release assembler never receives the zone-binding secret or raw database identity.

## Ready verdict

A manifest may return:

```text
READY_FOR_EXPLICIT_ACTIVATION_GATES
```

only when all required source gates are clean.

### PostgreSQL

```text
schema = BYBIT_DEMO_POSTGRES_BOOTSTRAP_V4
mode = verify
status = VERIFIED_READY
passed = true
schema_mutation_performed = false
logical_database_identity_verified = true
v119-v124 relations = ready
required durability/append-only/anti-reset/immutability guards = ready
```

### Connected preflight

```text
status = READY_FOR_MANUAL_OPERATOR_APPROVAL
reasons = []
fixed_egress_required = true
read_only_api_key_verified = true
api_key_ip_binding_present = true
preflight_only = true
trade_actionable = false
```

An existing open canonical trade returns `EXISTING_TRADE_MANAGEMENT_REQUIRED` and is not new-entry readiness.

### Trading credential

```text
status = READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL
write_enabled_verified = true
ip_binding_present = true
personal_key_type_verified = true
uta_enabled = true
ContractTrade Order/Position readiness = true
least_privilege_contract_only = true
credential namespaces distinct = true
authenticated_get_only = true
order_write_performed = false
```

### Same Demo account

```text
status = VERIFIED_SAME_ACCOUNT
passed = true
same_user_id = true
same_parent_uid = true
same_master_scope = true
authenticated_get_only = true
order_write_performed = false
```

The same-account evidence must carry the exact readiness `git_sha`. Any identity mismatch blocks ARM readiness.

### v121 control plane

The readiness baseline deliberately requires:

```text
mode = HALTED
new_entry_allowed = false
```

The product is infrastructure-ready while still safely halted. ARM remains a later explicit, short-lived operator action.

## Manifest and sidecar

The readiness artifact is:

```text
artifacts/bybit-demo-activation-readiness.json
```

and the same protected run also creates:

```text
artifacts/bybit-demo-operational-zone-binding.json
```

The readiness artifact contains exact Git SHA, SHA-256 of source evidence, bounded statuses, `demo_account_identity_verified`, final reasons/verdict and its canonical manifest hash.

The V2 zone sidecar contains only opaque HMAC resource bindings and verification booleans. Neither artifact contains raw database UUID, DSN, API credentials, user IDs, balances, quantities, prices or order identities.

## Intended activation sequence

```text
v119-v124 activation readiness PASS while HALTED
-> v124-backed zone sidecar from the same run
-> explicit one-time v122 session-risk initialization if needed
-> pre-entry persistent-supervisor IDLE proof
-> explicit short-lived v121 ARM
-> exact short-lived operator approval
-> one protected Demo entry
-> explicit HALT
-> controlled recovery drill when required
-> exact-head release assembly from V2 same-zone sidecars
```

Session initialization must be flat/HALTED and read current opening equity from authenticated Demo account state. It must never silently recreate a missing ledger during normal worker startup.

## Qualification versus real readiness

Pull-request qualification proves code, PostgreSQL lifecycle and manifest logic using isolated/synthetic evidence. Protected/manual jobs remain skipped on pull requests.

A real `READY_FOR_EXPLICIT_ACTIVATION_GATES` requires a manual run on the protected self-hosted Demo runner after v124 has been explicitly applied to the intended operational database. The same-account and V2 zone proofs must pass for the actual configured resources.

Mainnet remains read-only. No pull-request result, including this readiness workflow, is trade authorization.
