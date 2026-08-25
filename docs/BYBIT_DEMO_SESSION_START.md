# Bybit Demo session-start gate

This gate is the one-time operational boundary that creates the durable v122 Demo risk session. Normal worker startup must never create or reset that session.

## Why it exists

A persistent trading product cannot treat process startup as a new risk day or a new risk session. Restarting a runner, container or process must preserve:

- opening equity;
- equity high-water;
- terminal all-in PnL history;
- execution costs;
- consecutive-loss history;
- the exact durable ledger revision.

Therefore a missing v122 ledger blocks a future worker. It is initialized only through this explicit gate.

## Manual workflow

Use:

```text
bybit-demo-session-start
```

Operational execution is restricted to:

```text
runs-on: [self-hosted, bybit-demo]
environment: bybit-demo
```

There is no schedule.

The workflow exposes only:

```text
status
initialize
```

There is intentionally no reset, clear, truncate, rollover or takeover mode.

## `status`

`status` receives only:

```text
BYBIT_DEMO_DATABASE_DSN
```

It performs no Bybit authentication and no schema mutation. It verifies the v119-v122 bootstrap contract and then verifies the v122 active checkpoint against the append-only terminal outcome journal.

Possible operational meanings include:

```text
NOT_INITIALIZED   -> future worker must remain blocked
INITIALIZED       -> durable risk session exists and can be resumed
BLOCKED           -> schema or durable ledger verification failed
```

Exact opening equity is not emitted in the sanitized artifact.

## `initialize`

Initialization requires the exact phrase:

```text
INITIALIZE_BYBIT_DEMO_SESSION_RISK
```

It receives only:

```text
BYBIT_DEMO_DATABASE_DSN
BYBIT_DEMO_READONLY_API_KEY
BYBIT_DEMO_READONLY_API_SECRET
```

It does **not** receive the Demo trading credential or any mainnet credential.

Before creating the singleton, the coordinator requires all of the following:

```text
v119-v122 VERIFIED_READY
fixed-egress Demo read-only client
IP-bound read-only API key
positive authenticated Demo equity
v121 HALTED
no runtime lease
no active excursion checkpoint
no existing v122 session
no exchange position
no pending exchange order
explicit operator id + reason
exact checked-out 40-char Git SHA
```

The v122 opening equity is taken from an authenticated wallet read in the same bounded operation and is never included as an exact value in the output artifact.

## Race control

The initializer acquires PostgreSQL transaction advisory lock `122001` and locks the canonical runtime lease, active checkpoint, v121 control and v122 singleton tables before the final exchange checks and insert.

This prevents bot-side lease/checkpoint/ARM activation from racing between the flat/HALTED checks and the v122 singleton insert.

Exchange state is outside the PostgreSQL transaction. A human could still act directly in Bybit. To reduce that irreducible external race, the initializer performs a fresh fixed-egress connected preflight and then directly re-reads open positions and open orders immediately before the insert.

If either final exchange read is non-flat, initialization fails closed.

## Repeat initialization

If the singleton already exists, `initialize` is rejected with:

```text
DEMO_SESSION_ALREADY_INITIALIZED
```

The result still reports that the existing session is initialized and worker-resumable, but it does not replace opening equity or create a new risk history.

Changing the supplied equity/account state cannot replace the existing ledger.

## Sanitized evidence

The workflow writes:

```text
artifacts/bybit-demo-session-risk.json
```

Successful first initialization reports only bounded metadata such as:

- status `INITIALIZED_NOW`;
- ledger SHA-256 revision;
- outcome count;
- positive-opening-equity boolean;
- SHA-256 of the sanitized connected preflight;
- exact Git SHA;
- derived session-start SHA-256 id;
- safety/capability booleans.

It does not expose API keys, API secret, DSN, IP address, exact equity, exact balance, position quantity, prices, order IDs or execution IDs.

## Safety boundary

Every result remains:

```text
fixed_egress_required=true
explicit_operator_action_required=true
automatic_reset_allowed=false
trading_credential_required=false
order_write_performed=false
order_writes_supported=false
live_mainnet_order_routing_allowed=false
```

The session-start gate is risk-state infrastructure, not permission to trade.

## Product sequence after this gate

```text
activation readiness PASS while HALTED
-> session status
-> if absent: explicit one-time initialize
-> future worker startup: load/resume v122 only
-> current wallet high-water CAS
-> reconcile terminal all-in outcomes into v122
-> derive current risk state
-> exact candidate + exact short-lived approval
-> explicit short-lived ARM
-> only then may a protected Demo worker create new exposure
```

A real operational initialization cannot be claimed from pull-request CI. It requires the protected fixed-egress self-hosted workflow with the actual Demo database and read-only credential.

Mainnet remains read-only.
