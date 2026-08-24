# Bybit Demo trading credential preflight

This gate verifies the shape of the **future write-enabled Bybit Demo trading credential** before any order-capable worker is allowed to use it.

It is intentionally GET-only. Passing this preflight is not permission to trade and does not prove that an order can be created.

## Official Bybit contract used by this gate

The probe calls exactly:

```text
GET https://api-demo.bybit.com/v5/user/query-api
```

Bybit documents Demo Trading as an isolated environment using `https://api-demo.bybit.com`. Bybit also documents `/v5/user/query-api` as available to API keys and returns key identity, `readOnly`, IP bindings, key `type`, UTA status and the permission map.

References:

- https://bybit-exchange.github.io/docs/v5/demo
- https://bybit-exchange.github.io/docs/v5/user/apikey-info

The inherited transport exposes only authenticated HTTPS GET. The credential inspector has no order creation, amend, cancel or trading-stop method.

## Required credential shape

A future Demo worker credential is accepted by this preflight only when all of the following are true:

```text
readOnly = 0
at least one concrete IP binding
type = 1          # personal key
uta = 1           # Unified Trading Account
ContractTrade permissions = exactly {Order, Position}
all other permission categories are empty
```

The least-privilege rule is fail-closed. Any current or future non-empty permission category outside `ContractTrade` blocks the credential. A Demo worker does not need Wallet transfer/withdrawal, Spot, Options, Exchange, Earn or other unrelated authority.

## Credential namespace isolation

The future trading key must not reuse either:

- the dedicated Bybit Demo read-only operational key; or
- the dedicated Bybit mainnet read-only key.

The credential-preflight workflow does **not** receive those raw keys. It compares the trading-key SHA-256 against two separately configured reference fingerprints:

```text
BYBIT_DEMO_READONLY_API_KEY_SHA256
BYBIT_MAINNET_READONLY_API_KEY_SHA256
```

These are GitHub environment/repository variables, not API credentials. The result artifact does not emit either fingerprint.

The only API credential pair available to this workflow is:

```text
BYBIT_DEMO_TRADING_API_KEY
BYBIT_DEMO_TRADING_API_SECRET
```

The API secret is used only to sign the authenticated GET request.

## Fixed-egress execution zone

An IP-bound write-enabled key must not be tested from an ephemeral GitHub-hosted public IP. The operational job therefore requires:

```text
runs-on: [self-hosted, bybit-demo]
environment: bybit-demo
```

The `bybit-demo` self-hosted runner is part of the future execution trust boundary and must use a stable outbound IP that is explicitly included in the Bybit API-key IP bindings. The protected GitHub environment should require reviewer approval and should own the two trading-key secrets.

Pull-request qualification still runs on `ubuntu-latest`, but it uses only a fake transport and never receives operational credentials.

The future Demo worker should run in the same or an equivalently controlled fixed-egress zone. Moving the credential from the preflight zone to an unrelated runtime with another egress IP invalidates this readiness evidence.

## Workflow

Manual operational workflow:

```text
bybit-demo-trading-credential-preflight
```

There is no schedule and no autonomous retry loop.

Pull-request qualification uses a fake GET transport and no secrets. It proves:

- exact `/v5/user/query-api` request path;
- no mutation method on the inspector;
- write-enabled/read-only interpretation;
- concrete IP validation;
- personal-key and UTA requirements;
- exact `ContractTrade: Order + Position` least privilege;
- rejection of Wallet/Withdraw, Spot and unknown future permissions;
- fingerprint-based namespace isolation;
- sanitized output that excludes key, secret, IP and fingerprints.

## Result

A clean result is:

```text
READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL
```

A failed condition produces `BLOCKED` plus one or more bounded reason codes. Protocol/configuration failures produce a sanitized failure artifact containing the exception type only.

The output deliberately excludes:

- API key and secret;
- UID/subaccount identity;
- IP values;
- raw permission map;
- notes;
- API-key fingerprints;
- account balances;
- positions/orders;
- any mainnet credential material.

Every result remains:

```text
authenticated_get_only=true
order_write_performed=false
order_writes_supported=false
live_mainnet_order_routing_allowed=false
```

## What PASS proves — and does not prove

PASS proves that the configured credential authenticated successfully from the protected fixed-egress Demo zone on the Demo API-key-info endpoint and has the required least-privilege metadata shape.

PASS does not prove:

- PostgreSQL v119-v121 is deployed;
- the read-only connected account preflight is clean;
- v121 is currently ARMED;
- a particular strategy candidate is approved;
- `order/create` is operational;
- protection and terminal reconciliation have succeeded on a real Demo order;
- any mainnet write capability exists.

Those are separate gates.

## Activation order

The product activation sequence is therefore:

```text
1. provision protected self-hosted bybit-demo runner with fixed egress
2. PostgreSQL v119-v121 -> VERIFIED_READY
3. connected Demo read-only preflight -> READY_FOR_MANUAL_OPERATOR_APPROVAL
4. Demo trading credential preflight -> READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL
5. v121 control plane -> explicit short-lived ARM only when needed
6. exact short-lived operator trade approval
7. future protected Demo worker invokes the canonical single-writer runtime
8. immutable authorization -> final ARM recheck -> Demo order submit
9. protection/recovery -> checkpoint management -> terminal reconciliation/evidence
```

A future worker must consume the dedicated trading key only at the final execution boundary. It must never reuse the read-only Demo key and must never import a mainnet order-routing surface.

Mainnet remains read-only.
