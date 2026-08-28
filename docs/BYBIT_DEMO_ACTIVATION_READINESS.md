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
1. PostgreSQL v119-v123 verify only
2. fixed-egress connected read-only Demo preflight
3. GET-only dedicated Demo trading credential preflight
4. GET-only proof that read-only and trading credentials belong to one exact Demo account
5. v121 control status read
6. fail-closed manifest assembly
```

The PostgreSQL artifact is `BYBIT_DEMO_POSTGRES_BOOTSTRAP_V3`; `VERIFIED_READY` requires the complete v119-v123 contract and its required append-only/anti-reset guards.

The source steps write sanitized evidence artifacts. The final assembler reads those files, validates their schemas and safety flags, hashes the exact bytes with SHA-256, binds the manifest to the checked-out Git commit, and emits one readiness artifact.

## Same-account proof

Bybit `GET /v5/user/query-api` identifies the account that owns an API key. The readiness workflow performs two authenticated GET-only identity reads and requires exact equality of:

```text
userID
parentUid
isMaster
```

The raw values are compared only in memory and are never serialized. The sanitized artifact contains only booleans such as `same_user_id`, `same_parent_uid`, `same_master_scope` and final status `VERIFIED_SAME_ACCOUNT` or `BLOCKED`.

This closes a critical configuration failure mode where accounting/preflight could read Demo account A while the write-capable key belonged to Demo account B.

The same guard is repeated by the operator-approved entry process and persistent supervisor before either process constructs its order-capable client. Readiness is therefore not the only line of defense.

## Step-scoped secrets

Credentials are not placed in a job-level environment.

- PostgreSQL verify receives only `BYBIT_DEMO_DATABASE_DSN`.
- Connected preflight receives the DSN plus the Demo read-only key/secret.
- Trading credential preflight receives the Demo trading key/secret plus read-only/mainnet namespace fingerprints.
- Same-account identity proof receives the Demo read-only and trading key/secret pairs, but no database DSN and no order-capable client. It performs authenticated GET only and emits no UID or key material.
- Control status receives only the DSN.
- Manifest assembly receives no credential secret.

The identity comparison is the only readiness step that intentionally sees both Demo credential pairs because exact account equality cannot be proven from the existing redacted artifacts alone. Its implementation exposes no mutation method.

## Ready verdict

A manifest may return:

```text
READY_FOR_EXPLICIT_ACTIVATION_GATES
```

only when all of these are true.

### PostgreSQL

```text
schema = BYBIT_DEMO_POSTGRES_BOOTSTRAP_V3
mode = verify
status = VERIFIED_READY
passed = true
schema_mutation_performed = false
v119-v123 relations = ready
required durability/append-only/anti-reset guards = ready
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

An existing open canonical trade returns `EXISTING_TRADE_MANAGEMENT_REQUIRED` and therefore is not new-entry activation readiness.

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

The same-account evidence must carry the exact readiness `git_sha`. A mismatched SHA or any identity mismatch blocks ARM readiness.

### v121 control plane

The readiness baseline deliberately requires:

```text
mode = HALTED
new_entry_allowed = false
```

The product is considered infrastructure-ready while still safely halted. ARM remains a later explicit, short-lived operator action.

## Manifest

The final artifact is:

```text
artifacts/bybit-demo-activation-readiness.json
```

It contains the exact Git commit SHA, SHA-256 of each source evidence file including the same-account proof, bounded source statuses, `demo_account_identity_verified`, final reasons/verdict, a SHA-256 over the canonical readiness manifest, and booleans proving ARM/order/mainnet actions were not performed or enabled.

It does not embed source evidence contents, API keys, API secrets, user IDs, parent UIDs, IP addresses, DSN, exact balances, quantities, prices, order IDs or execution IDs.

A ready manifest remains explicitly:

```text
demo_account_identity_verified = true
operator_action_required = true
arm_performed = false
trade_actionable = false
order_write_performed = false
order_writes_supported = false
live_mainnet_order_routing_allowed = false
```

## Intended activation sequence

```text
v119-v123 activation readiness PASS while HALTED
-> explicit one-time v122 session-risk initialization if no durable ledger exists
-> thereafter always load/resume the v122 ledger on restart
-> pre-entry persistent-supervisor IDLE proof
-> explicit short-lived v121 ARM
-> exact short-lived operator approval
-> one protected Demo entry
-> explicit HALT
-> controlled recovery drill when required
```

Session initialization must be flat/HALTED and read current opening equity from authenticated Demo account state. It must never silently recreate a missing ledger during normal worker startup.

## Qualification versus real readiness

Pull-request qualification proves code, PostgreSQL lifecycle and manifest logic using isolated/synthetic evidence. Protected/manual jobs remain skipped on pull requests.

A real `READY_FOR_EXPLICIT_ACTIVATION_GATES` requires a manual run on the protected self-hosted Demo runner with configured operational database, network and credentials, and the same-account GET-only proof must pass for the actual configured credentials.

Mainnet remains read-only. No pull-request result, including this readiness workflow, is trade authorization.
