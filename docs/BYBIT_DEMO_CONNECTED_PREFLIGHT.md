# Connected Bybit Demo preflight

This gate is the first connected step between the repository and a real Bybit Demo account. It is deliberately **read-only** and cannot submit, amend, cancel, protect, or close an order.

## Why this gate exists

The qualified execution stack now has durable PostgreSQL runtime state (v119) and immutable approval/provenance/terminal evidence (v120). Before any write-enabled Demo credential is introduced, the system must prove that the real Demo account and durable state agree.

The preflight verifies:

- authenticated access to `api-demo.bybit.com` through a GET-only account client;
- the actual credential reports `readOnly=1` through authenticated `GET /v5/user/query-api`;
- whether an IP binding exists, without publishing any bound IP values;
- positive usable Demo account equity and account metadata without publishing exact balances;
- positive available balance before a new-entry state can be considered ready;
- current open linear USDT positions;
- current open linear USDT orders through authenticated `GET /v5/order/realtime`;
- presence of all required v119/v120 PostgreSQL relations;
- presence of all three v120 append-only audit triggers;
- absence of an already-held canonical runtime lease;
- one-position maximum for the current canonical runtime;
- no pending open order when there is no exchange position;
- exact agreement between an open exchange position and the durable active checkpoint symbol/side;
- exact current quantity and average entry agreement with the durable checkpoint;
- existence of an exchange execution carrying the checkpoint's exact deterministic `ASTRA-DEMO-*` entry `orderLinkId`, symbol, and side;
- any open order accompanying an existing canonical position must be reduce-only, on the same symbol, and on the opposite side so it cannot increase exposure.

These checks prevent a stale pending entry, a manual or unrelated Demo position, or an unsafe same-side/non-reduce-only order from being mistaken for canonical managed state.

It never imports or instantiates `BybitDemoOrderClient` and the account client exposes no `place_order` or `cancel_order` method. Credential capability, account state, pending orders, position state, and entry identity are verified through authenticated GET endpoints only.

## Credential isolation

Create a **separate Bybit Demo read-only API key** for this gate. Do not reuse the future write-enabled Demo trading key. The preflight does not trust the secret name: it asks Bybit for the current key metadata and blocks unless the exchange returns `readOnly=1`.

GitHub Secrets required by the manual workflow:

```text
BYBIT_DEMO_READONLY_API_KEY
BYBIT_DEMO_READONLY_API_SECRET
BYBIT_DEMO_DATABASE_DSN
```

`BYBIT_DEMO_DATABASE_DSN` must point to the PostgreSQL database where migrations v119 and v120 are already applied. The preflight performs only read transactions and does not migrate schemas.

An IP binding is reported as a boolean operational signal but is not a hard gate for the GitHub-hosted runner because its outbound address is not assumed to be static. If a stable egress runner is introduced later, IP binding should become mandatory there.

A future write-enabled operator-approved worker must use a different credential namespace and a protected GitHub Environment. That later worker is not introduced by this gate.

## Manual operation

Run the GitHub Actions workflow:

```text
bybit-demo-connected-preflight
```

It has `workflow_dispatch` only for connected operation. There is no schedule and no autonomous execution path.

The operational step writes one sanitized artifact:

```text
artifacts/bybit-demo-connected-preflight.json
```

The artifact intentionally excludes API keys, secrets, DSN, exact equity, exact available balance, wallet amounts, position quantities, entry prices, execution identifiers, open-order identities and quantities, and bound IP values. It retains only the minimum operational status, counts and symbols needed to diagnose readiness.

## Status meanings

### `READY_FOR_MANUAL_OPERATOR_APPROVAL`

The supplied credential is verified read-only, available balance is positive, the account has no open position and no pending open order, the durable active checkpoint is empty, the canonical runtime lease is free, and v119/v120 guards are present. This status means only that infrastructure is consistent enough to proceed to a separate manual operator-approval step. It does **not** make any market opportunity actionable.

### `EXISTING_TRADE_MANAGEMENT_REQUIRED`

The supplied credential is verified read-only. Exactly one open Demo position exists and its symbol, side, current quantity and average entry agree with the durable active checkpoint, and the checkpoint's deterministic entry `orderLinkId` is present in exchange execution history for the same trade identity. Any currently open order is constrained to reduce-only protection on the same symbol and opposite side. A future connected runtime may manage/reconcile that existing trade, but a new entry must not be started.

### `BLOCKED`

At least one fail-closed condition exists, including:

- supplied Demo credential is not actually read-only;
- v119/v120 schema missing;
- append-only v120 triggers missing;
- canonical runtime lease already present;
- more than one open position;
- no positive available balance for a clean new-entry state;
- pending open order while no position exists;
- exchange position without durable checkpoint;
- durable checkpoint without exchange position;
- checkpoint symbol mismatch;
- checkpoint side mismatch;
- checkpoint current quantity mismatch;
- checkpoint average entry mismatch;
- exact checkpoint entry execution not found on the exchange;
- open order on a symbol outside the active canonical position;
- non-reduce-only open order while a position exists;
- open order on the same side as the position, which could increase exposure.

A blocked preflight must be reconciled before any write-enabled Demo worker is introduced.

## Safety boundary

Every preflight output remains:

```text
preflight_only=true
trade_actionable=false
order_writes_supported=false
live_mainnet_order_routing_allowed=false
```

Mainnet connectivity is unaffected and remains in the separately qualified read-only boundary.
