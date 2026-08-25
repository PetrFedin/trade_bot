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
1. PostgreSQL v119-v122 verify only
2. fixed-egress connected read-only Demo preflight
3. GET-only dedicated Demo trading credential preflight
4. v121 control status read
5. fail-closed manifest assembly
```

The PostgreSQL artifact remains `BYBIT_DEMO_POSTGRES_BOOTSTRAP_V2` for backward-compatible parsing, but `VERIFIED_READY` now requires the complete v119-v122 contract, including both v122 session-risk relations and all four anti-reset/append-only triggers.

The first four steps each write their existing sanitized evidence artifact. The final assembler reads those files, validates their schemas and safety flags, hashes the exact bytes with SHA-256, binds the manifest to the checked-out Git commit, and emits one readiness artifact.

## Step-scoped secrets

Credentials are not placed in a job-level environment.

- PostgreSQL verify receives only `BYBIT_DEMO_DATABASE_DSN`.
- Connected preflight receives the DSN plus the Demo read-only key/secret.
- Trading credential preflight receives the Demo trading key/secret plus read-only namespace fingerprints.
- Control status receives only the DSN.
- Manifest assembly receives no credential secret.

This preserves the existing separation between database, read-only account inspection and future trading-credential inspection while still producing a single operator-facing verdict.

## Ready verdict

A manifest may return:

```text
READY_FOR_EXPLICIT_ACTIVATION_GATES
```

only when all of these are true:

### PostgreSQL

```text
schema = BYBIT_DEMO_POSTGRES_BOOTSTRAP_V2
mode = verify
status = VERIFIED_READY
passed = true
schema_mutation_performed = false
v119-v122 relations = ready
v119-v122 durability/append-only triggers = ready
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

It contains exact Git commit SHA, SHA-256 of each source evidence file, bounded source statuses, final reasons/verdict, a SHA-256 over the canonical readiness manifest, and booleans proving ARM/order/mainnet actions were not performed or enabled.

It does not embed source evidence contents, API keys, secrets, IP addresses, DSN, exact balances, quantities, prices, order IDs or execution IDs.

A ready manifest remains explicitly:

```text
operator_action_required = true
arm_performed = false
trade_actionable = false
order_write_performed = false
order_writes_supported = false
live_mainnet_order_routing_allowed = false
```

## Intended activation sequence

```text
v119-v122 activation readiness PASS while HALTED
-> explicit v122 session-risk initialization if no durable ledger exists
-> thereafter always load/resume v122 ledger on restart
-> operator selects/revalidates exact candidate
-> exact short-lived trade approval
-> explicit short-lived ARM (which reruns connected preflight)
-> future protected Demo execution worker
```

The future session initialization gate must be flat/HALTED and read current opening equity from authenticated Demo account state. It must never silently recreate a missing ledger during normal worker startup.

## Qualification versus real readiness

Pull-request qualification proves only code, PostgreSQL lifecycle, and manifest logic using isolated/synthetic evidence.

A real `READY_FOR_EXPLICIT_ACTIVATION_GATES` requires a manual run on the protected self-hosted Demo runner with configured operational database, network and credentials.

Until that real operational evidence exists, the write-enabled Demo worker remains intentionally unwired.

Mainnet remains read-only.
