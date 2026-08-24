# Connected Bybit Demo preflight

This gate is the connected read-only boundary between the repository and a real Bybit Demo account. It cannot submit, amend, cancel, protect, or close an order.

Operationally it now runs only inside the protected fixed-egress `bybit-demo` zone described in `docs/BYBIT_DEMO_OPERATIONAL_ZONE.md`.

## Why this gate exists

Before any write-enabled Demo worker may exist, the system must prove that the authenticated Demo account, durable PostgreSQL state, network binding and exchange state agree.

The preflight verifies:

- authenticated access to exact host `api-demo.bybit.com` through a GET-only client;
- the actual credential reports `readOnly=1` through authenticated `GET /v5/user/query-api`;
- the read-only key has at least one concrete IP binding;
- every returned IP binding is a valid IPv4/IPv6 address and duplicate bindings are rejected;
- no bound IP value is published in artifacts;
- positive Demo account equity and positive available balance without publishing exact amounts;
- current open linear USDT positions and orders;
- required v119/v120 PostgreSQL relations and append-only audit triggers;
- absence of an already-held canonical runtime lease for a clean new-entry state;
- one-position maximum for the canonical runtime;
- no pending open order when there is no exchange position;
- exact agreement between an open exchange position and the durable active checkpoint symbol/side/quantity/average entry;
- existence of an exchange execution carrying the checkpoint's exact deterministic `ASTRA-DEMO-*` entry `orderLinkId`;
- any open order accompanying an existing canonical position must be reduce-only, on the same symbol and opposite side.

These checks prevent a stale pending entry, a manual/unrelated Demo position, an unsafe same-side order, or an unbound credential from being mistaken for canonical ready state.

## Credential isolation and fixed egress

Use a **separate Bybit Demo read-only API key**. Do not reuse the future write-enabled Demo trading key.

Operational GitHub secrets:

```text
BYBIT_DEMO_READONLY_API_KEY
BYBIT_DEMO_READONLY_API_SECRET
BYBIT_DEMO_DATABASE_DSN
```

The operational workflow runs with:

```text
runs-on: [self-hosted, bybit-demo]
environment: bybit-demo
```

The runner must have stable outbound egress and the read-only key must be bound to an allowed concrete IP for that operational zone. A missing/wildcard-only binding produces `BLOCKED`; malformed or duplicate IP metadata is treated as a protocol/configuration failure.

Pull-request qualification remains on GitHub-hosted runners and never receives operational secrets.

## Manual operation

Run:

```text
bybit-demo-connected-preflight
```

There is no schedule and no autonomous execution path.

The operational step writes:

```text
artifacts/bybit-demo-connected-preflight.json
```

The artifact excludes API keys, secrets, DSN, exact balances, position quantities, entry prices, execution/order identifiers and bound IP values. It retains only bounded status, counts, symbols and safety booleans.

## Status meanings

### `READY_FOR_MANUAL_OPERATOR_APPROVAL`

The credential is verified read-only **and IP-bound**, available balance is positive, there is no open position/order, the durable checkpoint is empty, the runtime lease is free, and required durable guards are present. This status means infrastructure is consistent enough to proceed through the later operator gates; it does not make a market opportunity actionable.

### `EXISTING_TRADE_MANAGEMENT_REQUIRED`

Exactly one canonical Demo position exists and its exchange state matches the durable checkpoint and entry execution identity. Any open order is constrained to reduce-only protection. The trade may be managed/reconciled, but a new entry must not start.

The operational fixed-egress wrapper still requires the read-only credential itself to be concretely IP-bound.

### `BLOCKED`

Fail-closed conditions include:

- Demo credential is not actually read-only;
- Demo read-only key has no concrete IP binding;
- required PostgreSQL schema/triggers are missing;
- canonical runtime lease is already present;
- multiple positions exist;
- available balance is not positive for a clean new-entry state;
- open order exists without a position;
- position/checkpoint state is missing or mismatched;
- exact checkpoint entry execution is absent;
- an open order can increase exposure or belongs to another symbol.

A blocked preflight must be reconciled before ARM or a future write-enabled Demo worker may proceed.

## ARM relationship

The v121 operational control path invokes this same fixed-egress preflight in the ARM invocation. It then performs a second defense-in-depth check requiring:

```text
READY_FOR_MANUAL_OPERATOR_APPROVAL
read_only_api_key_verified=true
api_key_ip_binding_present=true
reasons=[]
order_writes_supported=false
live_mainnet_order_routing_allowed=false
```

The operational `FixedEgressPostgresBybitDemoControlPlane` enforces that contract before durable ARM persistence.

## Safety boundary

Every preflight remains:

```text
preflight_only=true
trade_actionable=false
order_writes_supported=false
live_mainnet_order_routing_allowed=false
```

Mainnet remains in its separate read-only boundary.