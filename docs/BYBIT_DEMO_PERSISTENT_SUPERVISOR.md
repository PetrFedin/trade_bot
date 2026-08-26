# Bybit Demo persistent supervisor

## Product boundary

The persistent supervisor is the long-running management half of the Bybit Demo product. It is not an autonomous trader and it has no selector, approval input, or new-entry capability.

The product is intentionally split into two authorities:

1. **Operator-approved entry action** creates new Demo exposure only through the existing v121 ARM, fresh approval, immutable pre-submit authorization, and canonical single-writer runtime.
2. **Persistent supervisor** manages, reduces, reconciles, and durably closes an already-open Demo trade until the excursion checkpoint is terminally acknowledged.

A missing active v119 excursion checkpoint is `IDLE_NO_ACTIVE_TRADE`. It is never interpreted as permission to scan for or enter a trade. The supervisor injects a hard-block entry executor into the canonical runtime so a race where the checkpoint disappears after the precheck still cannot create new exposure.

Mainnet order routing is not present in this composition root.

## Durable state

The supervisor treats PostgreSQL as authoritative:

- v119 — canonical runtime lease and active excursion checkpoint;
- v120 — immutable terminal evidence and approval/provenance history;
- v121 — operator control plane used by the separate entry action;
- v122 — initialized session-risk ledger, terminal all-in outcomes, and wallet-equity high-water;
- v123 — immutable audit for controlled recovery of an orphaned v119 runtime lease.

Every active cycle reads the real Demo wallet equity through the read-only credential. The v122 observer advances the high-water with CAS semantics before trade management runs. A process restart therefore cannot forget an unrealized session peak and understate subsequent drawdown.

Terminal completion remains ordered as:

`terminal evidence -> v122 all-in session-risk commit -> exact excursion checkpoint ACK`

No same-invocation replacement trade is allowed.

## Session-risk emergency action

An open trade is evaluated against the durable session-risk state before ordinary ratchet/max-hold management. If the policy returns `flatten_required`, the supervisor routes to the session-risk flatten executor first.

The executor:

- reloads the exact active checkpoint;
- reloads the real broker position;
- rejects exposure larger than the durable initial quantity;
- obtains a fresh Demo quote and current Demo instrument limits;
- submits exactly one deterministic **reduce-only** market close for the current residual quantity;
- never automatically retries an ambiguous mutation;
- requires independent post-submit position reconciliation;
- leaves terminal fill/accounting/evidence reconciliation to subsequent canonical cycles.

All write policies are disabled by default in library code. The launchable operational composition explicitly enables only stop tightening, max-hold close, and session-risk flatten for the already open Demo trade.

## Credential separation

The service requires separate credential namespaces:

- `BYBIT_DEMO_TRADING_API_KEY`
- `BYBIT_DEMO_TRADING_API_SECRET`
- `BYBIT_DEMO_READONLY_API_KEY`
- `BYBIT_DEMO_READONLY_API_SECRET`
- `BYBIT_MAINNET_READONLY_API_KEY_SHA256`
- `BYBIT_DEMO_DATABASE_DSN`

The supervisor does **not** need the mainnet API key or secret. Only the SHA-256 fingerprint of the mainnet read-only key is supplied so the Demo trading credential can prove namespace separation.

Before an order-capable client is used, startup performs:

1. read-only v119-v123 PostgreSQL schema verification;
2. connected Demo read-only account/database reconciliation;
3. GET-only inspection of the trading credential requiring write-enabled Demo key, IP binding, personal key type, UTA, exactly ContractTrade `Order` + `Position`, and no unrelated permission;
4. cryptographic distinction from both Demo read-only and mainnet read-only key identities;
5. load of the already initialized v122 session-risk singleton.

A failed startup gate produces only a sanitized error type in the status artifact.

## Running one cycle

Use this for deployment smoke tests and operational diagnosis:

```bash
python -m tools.run_bybit_demo_persistent_supervisor \
  --mode once \
  --output artifacts/bybit-demo-persistent-supervisor.json
```

The GitHub workflow `bybit-demo-persistent-supervisor` exposes this mode only through the protected `bybit-demo` environment on a fixed-egress self-hosted runner. Pull-request qualification never uses trading secrets and never writes exchange orders.

## Running as a service

The long-running process is:

```bash
python -m tools.run_bybit_demo_persistent_supervisor \
  --mode loop \
  --interval-seconds 5 \
  --output /var/lib/astra/bybit-demo-supervisor.json
```

Run it under the production service manager or container orchestrator, not as an endless GitHub Actions job. `SIGTERM` and `SIGINT` request a graceful stop between cycles. The canonical runtime lease is acquired and released inside each cycle; it is not held during sleep, so a separate operator-approved entry action can obtain the single-writer lease while the supervisor is idle.

Normal loop states:

- `IDLE_NO_ACTIVE_TRADE` — no durable active trade; no entry attempted;
- `ACTIVE_TRADE_CYCLE` — active trade observed/managed/reconciled;
- `TERMINAL_HANDOFF_COMPLETE` — terminal evidence and v122 risk committed, checkpoint cleared;
- `RUNTIME_BUSY` — another legitimate canonical invocation owns the lease; the loop may retry later.

`BLOCKED` is a process-stop condition. Unknown session risk, malformed checkpoint, missing instrument, unsafe dependency, unresolved canonical runtime state, or other fail-closed conditions do not get retried as if they were harmless.

## Crash and controlled lease recovery

The v119 runtime lease has no TTL and no automatic stale takeover. This is deliberate. A hard process kill inside a canonical invocation can leave an orphaned lease, and startup connected preflight then blocks rather than starting a second writer.

The v123 recovery path closes that operational dead-end without introducing lease stealing. Use `bybit-demo-runtime-lease-recovery` or `tools.recover_bybit_demo_runtime_lease` only after the prior process is independently proven stopped. Recovery requires:

- latest v121 event exactly `HALT_NEW_ENTRIES`;
- exact SHA-256 fingerprint returned by recovery `inspect`;
- operator id and bounded reason;
- non-secret external process-stop evidence reference;
- exact phrase `RECOVER_BYBIT_DEMO_RUNTIME_LEASE`.

The v123 audit insert and exact v119 lease delete commit atomically. The raw owner token never leaves PostgreSQL. Any active excursion checkpoint remains untouched. Recovery does not ARM entries; the system remains HALTED until a later deliberate ARM action.

Never manually delete the lease, never recover by age, and never add automatic stale takeover.

## Qualification versus connected operation

A green pull request proves code, PostgreSQL behavior, safety invariants, regression compatibility, and deterministic unit/integration behavior. It does not prove that production secrets exist or that a real Bybit Demo account was connected.

Only a successful protected `workflow_dispatch` one-cycle run, or the deployed service itself, can produce connected operational evidence. Until such a run exists, report the product as **code-qualified but not connected-account-qualified**.
