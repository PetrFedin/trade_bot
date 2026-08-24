# Bybit Demo v121 control plane

The v121 control plane is the fail-closed operational boundary for **new** Bybit Demo exposure. It does not make trading autonomous and it does not enable mainnet writes.

## Product rule

The effective default is always:

```text
HALTED
```

No v121 schema, no control event, an invalid event, an expired ARM window, an unavailable control decision, or a failed connected preflight cannot become permission to open a position.

An existing canonical Demo trade is different: HALT blocks new non-reduce-only exposure, but it must not strand an already-open position. Existing-trade reads, protection updates, order cancellation/reconciliation and reduce-only close remain available through the managed-trade path.

## Durable state

Migration `migrations/v121/001_bybit_demo_control_plane.sql` creates append-only relation:

```text
astra_bybit_demo_control_event_v121
```

The journal supports only:

```text
ARM_NEW_ENTRIES
HALT_NEW_ENTRIES
```

UPDATE and DELETE are rejected by a database trigger. Public privileges are revoked.

For every ARM event the database retains:

- operator identity and reason;
- exact short-lived expiry;
- `READY_FOR_MANUAL_OPERATOR_APPROVAL` preflight status;
- observation timestamp, limited to 30 seconds before ARM;
- sanitized canonical connected-preflight JSON;
- SHA-256 of that canonical record.

The canonical preflight record does **not** contain API keys, secrets, DSN, IP values, exact balances, position quantities, entry prices, order IDs or execution IDs. Keeping this record in PostgreSQL means the ARM audit remains independently reviewable after short-retention GitHub Actions artifacts expire.

## ARM requirements

ARM is deliberately temporary. Default TTL is 120 seconds and the database hard limit is 300 seconds.

A new ARM can only be persisted when all of the following are true:

- the connected preflight status is exactly `READY_FOR_MANUAL_OPERATOR_APPROVAL`;
- the authenticated Bybit Demo API key used for preflight is verified read-only;
- v119/v120 durable state and append-only guards are ready;
- there is no canonical runtime lease;
- there is no active excursion checkpoint;
- there is no open exchange position;
- there is no open exchange order;
- Demo equity and available balance are positive;
- the preflight itself exposes no order-write or mainnet-routing capability;
- the preflight observation is fresh enough for the ARM transaction.

`EXISTING_TRADE_MANAGEMENT_REQUIRED` is never an ARM-eligible status.

The ARM transaction also acquires a PostgreSQL table lock that prevents a canonical runtime lease from being inserted while the idle-state check and ARM event are committed.

## Two entry gates

The operator-approved runtime checks control state twice.

### Gate 1: under canonical runtime lease

The canonical runtime first acquires its single-writer lease and checks the durable active checkpoint. Only if no active trade exists can the approved entry closure run. The first v121 ARM check is the first operation in that closure.

Therefore HALT is not consulted for normal management of an already-open checkpoint, but a new entry cannot start selection/account checks without ARM.

### Gate 2: immediately before the exchange entry mutation

The raw Demo client is wrapped with `ControlPlaneGuardedBybitDemoClient`. For a non-reduce-only `place_market_order`, the wrapper re-reads v121 immediately before delegating to the underlying Demo network client.

The durable approval-lineage wrapper remains outside that final guard. The ordering is intentionally:

```text
operator approval / exact identity
-> immutable pre-submit authorization persisted
-> v121 ARM rechecked
-> Demo order/create network mutation
```

If ARM disappears after authorization is persisted but before the network call, the network call is blocked. The authorization stays burned and becomes recovery-only evidence; it cannot be reused as resubmit permission.

No database/exchange architecture can make a PostgreSQL decision and an external HTTPS call one atomic distributed transaction. The two-gate design minimizes that unavoidable final TOCTOU interval and fails closed on every observable control-state loss.

## Manual workflow

Use:

```text
bybit-demo-control-plane
```

There is no schedule.

Modes:

```text
status
halt
arm
```

`status` is read-only.

`halt` requires exact confirmation:

```text
HALT_BYBIT_DEMO_NEW_ENTRIES
```

`arm` requires exact confirmation:

```text
ARM_BYBIT_DEMO_NEW_ENTRIES
```

and reruns the full connected read-only preflight in the same invocation before appending ARM.

Required secrets are limited to:

```text
BYBIT_DEMO_DATABASE_DSN
BYBIT_DEMO_READONLY_API_KEY
BYBIT_DEMO_READONLY_API_SECRET
```

The control workflow has no write-enabled trading credential and no mainnet credential.

## Operational sequence

```text
PostgreSQL v119-v121 VERIFIED_READY
-> connected read-only preflight
-> control status (normally HALTED)
-> explicit ARM with operator reason and short TTL
-> separately protected operator-approved Demo runtime
-> Gate 1 under runtime lease
-> exact approval + account/risk/fresh-quote checks
-> immutable authorization
-> Gate 2 immediately before non-reduce-only entry
-> exchange reconciliation + protection
-> durable active checkpoint management
-> terminal fill/PnL/funding reconciliation
-> terminal evidence
-> next entry remains subject to a new ARM
```

ARM does not mean "trade now". It only means that a separately operator-approved candidate may proceed through the rest of the existing fail-closed checks during the short window.

## What this PR does not claim

Code qualification does not prove that the operational `BYBIT_DEMO_DATABASE_DSN` exists, that the connected read-only Bybit Demo key exists, or that a real connected preflight has passed. Those are deployment evidence gates and must be observed from actual manual workflow runs.

This control plane also does not create the future write-enabled Demo worker. Introducing a trading credential before the database bootstrap, connected preflight and control-plane operational evidence are proven would weaken rather than strengthen the product.

Mainnet order routing remains prohibited and unchanged.
