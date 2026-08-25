# Bybit Demo session-start gate

This gate is the one-time operational boundary that atomically creates the durable v122 Demo risk session and its immutable v123 provenance. Normal worker startup must never create or reset that session.

## Why it exists

A persistent trading product cannot treat process startup as a new risk day or a new risk session. Restarting a runner, container or process must preserve:

- opening equity;
- equity high-water;
- terminal all-in PnL history;
- execution costs;
- consecutive-loss history;
- the exact durable ledger revision;
- who initialized the session, why, on which Git revision and against which connected-preflight evidence.

Therefore a missing v122 ledger blocks a future worker. A v122 ledger without valid v123 provenance also blocks worker readiness.

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

There is no schedule. The workflow exposes only:

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

It performs no Bybit authentication and no schema mutation. It verifies the complete v119-v123 bootstrap contract, verifies the v122 active checkpoint against the append-only terminal outcome journal, then verifies the v123 canonical provenance record and SHA-256.

Possible operational meanings include:

```text
NOT_INITIALIZED   -> future worker must remain blocked
INITIALIZED       -> durable risk session + provenance exist and can be resumed
BLOCKED           -> schema, ledger or provenance verification failed
```

A v122 row with missing/corrupt v123 provenance returns `BLOCKED`, with `session_initialized=true` but `worker_session_ready=false`.

Exact opening equity and operator reason are not emitted in the sanitized status artifact.

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

Before creating the session, the coordinator requires all of the following:

```text
v119-v123 VERIFIED_READY
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

The same PostgreSQL transaction creates one v123 provenance row containing:

```text
session_start_id = SHA256(canonical provenance record)
operator_id
reason
exact git_sha
sanitized connected-preflight SHA256
initial v122 ledger revision SHA256
fixed-egress / no-order-write safety markers
started_at
```

The v123 row has one identity per active session and database triggers reject UPDATE, DELETE and TRUNCATE.

## Race control

The initializer acquires PostgreSQL transaction advisory lock `122001` and locks the canonical runtime lease, active checkpoint, v121 control, v122 singleton and v123 provenance tables before the final exchange checks and atomic inserts.

This prevents bot-side lease/checkpoint/ARM activation or competing initialization from racing between the flat/HALTED checks and session creation.

Exchange state is outside the PostgreSQL transaction. A human could still act directly in Bybit. To reduce that irreducible external race, the initializer performs a fresh fixed-egress connected preflight and then directly re-reads open positions and open orders immediately before the v122/v123 inserts.

If either final exchange read is non-flat, initialization fails closed.

## Repeat initialization

If the singleton already exists, `initialize` is rejected with:

```text
DEMO_SESSION_ALREADY_INITIALIZED
```

The result still reports the verified existing session as initialized and worker-resumable. It preserves the **original** v123 `git_sha`, preflight SHA and session-start id; the new attempted Git SHA cannot overwrite historical provenance.

Changing supplied equity/account state cannot replace the existing ledger or provenance.

## Sanitized evidence

The workflow writes:

```text
artifacts/bybit-demo-session-risk.json
```

Successful first initialization reports only bounded metadata such as:

- status `INITIALIZED_NOW`;
- current ledger SHA-256 revision;
- outcome count;
- positive-opening-equity boolean;
- SHA-256 of the sanitized connected preflight;
- original exact Git SHA;
- immutable session-start SHA-256 id;
- safety/capability booleans.

It does not expose API keys, API secret, DSN, IP address, exact equity, exact balance, operator reason, position quantity, prices, order IDs or execution IDs.

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

The session-start gate is risk-state/audit infrastructure, not permission to trade.

## Product sequence after this gate

```text
activation readiness PASS while HALTED
-> session status
-> if absent: explicit one-time v122 + v123 initialize
-> future worker startup: verify v123 and load/resume v122 only
-> current wallet high-water CAS
-> reconcile terminal all-in outcomes into v122
-> derive current risk state
-> exact candidate + exact short-lived approval
-> explicit short-lived ARM
-> only then may a protected Demo worker create new exposure
```

Do not perform real session initialization from a v122-only deployment. Deploy/verify v123 first so the initial provenance can be recorded atomically rather than reconstructed later.

A real operational initialization cannot be claimed from pull-request CI. It requires the protected fixed-egress self-hosted workflow with the actual Demo database and read-only credential.

Mainnet remains read-only.
