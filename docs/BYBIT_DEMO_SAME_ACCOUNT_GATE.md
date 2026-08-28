# Bybit Demo same-account credential gate

## Purpose

The Demo read-only credential and the dedicated Demo trading credential must be different API keys, but they must resolve to the **same exact Bybit Demo account**.

Credential namespace separation alone is not sufficient. Without an account-identity check, a configuration error could read wallet/session state from account A while the order-capable key belongs to account B. That would invalidate sizing, reconciliation, protection and recovery assumptions before the first write.

## Authoritative identity source

The gate uses authenticated:

```text
GET https://api-demo.bybit.com/v5/user/query-api
```

for each configured Demo credential. The response must return the exact configured `apiKey`, an empty secret marker, and a valid account identity.

The following account fields must match exactly between the read-only and trading credentials:

```text
userID
parentUid
isMaster
```

`userID` identifies the exact account. `parentUid` and `isMaster` prevent a master/sub-account scope mismatch from being treated as equivalent.

## Privacy boundary

Raw `userID` and `parentUid` values are never serialized into readiness, supervisor, entry or release artifacts.

The public proof contains only:

```text
status = VERIFIED_SAME_ACCOUNT | BLOCKED
same_user_id = true | false
same_parent_uid = true | false
same_master_scope = true | false
authenticated_get_only = true
order_write_performed = false
order_writes_supported = false
live_mainnet_order_routing_allowed = false
```

Failure artifacts expose only bounded reason codes or the exception class. API keys, API secrets, raw UIDs and raw exchange responses are not emitted.

## Where the guard is enforced

### Activation readiness

`bybit-demo-activation-readiness` runs a dedicated GET-only same-account probe before assembling the readiness manifest. The readiness manifest cannot return `READY_FOR_EXPLICIT_ACTIVATION_GATES` unless the proof is `VERIFIED_SAME_ACCOUNT` and bound to the exact checked-out Git SHA.

The identity probe receives the two Demo credential pairs because comparison requires both authenticated account identities. It receives no PostgreSQL DSN and imports no order-capable client. The manifest assembler still receives only sanitized evidence files.

### Operator-approved entry

`tools/run_bybit_demo_operator_approved_entry.py` repeats same-account proof after the existing trading-credential preflight and **before** it constructs `OmsAwareBybitDemoStopRatchetClient`.

A mismatch raises a sanitized startup failure. The session store, OMS/recovery composition and order-capable client are not reached.

Successful operational entry evidence includes:

```text
demo_account_identity_verified = true
```

### Persistent supervisor

`tools/run_bybit_demo_persistent_supervisor.py` repeats the proof after connected/trading GET-only preflights and **before** it constructs `BybitDemoStopRatchetClient`.

This matters even when no new ENTRY is allowed: the supervisor can tighten protection or perform deterministic reduce-only safety exits for an already-open trade, so its accounting and write credentials must describe the same account.

Successful supervisor evidence includes:

```text
demo_account_identity_verified = true
```

## Release evidence

The exact-head operational release assembler requires `demo_account_identity_verified=true` in:

- activation readiness;
- persistent-supervisor proof, when that stage is present;
- operator-approved entry evidence, when that stage is present.

Legacy or manually fabricated artifacts without the field fail closed rather than being grandfathered into a later release stage.

## What this gate does not do

It does not:

- authorize ARM;
- create operator approval;
- change strategy, ranking, sizing or session-risk rules;
- submit, amend or cancel an order;
- expose mainnet order routing;
- prove that secrets/DSN remained unchanged across separate workflow runs.

Cross-run operational-zone binding is a separate provenance problem and should be handled with blinded resource binding rather than exposing raw API-key or DSN fingerprints.
