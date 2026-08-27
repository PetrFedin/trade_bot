# Operator-approved Bybit Demo execution

This layer connects one explicitly approved evidence-ranked review decision to the canonical Bybit Demo execution stack without enabling autonomous trading or any mainnet write path.

## Boundary

Mainnet remains read-only. There is no mainnet order client, no mainnet order mutation, and no path that turns ranked evidence into an autonomous order.

A new Demo entry requires independent evidence for all of the following:

1. protected fixed-egress execution on the existing `[self-hosted, bybit-demo]` runner and `bybit-demo` GitHub Environment;
2. an already-existing short-lived v121 `ARM_NEW_ENTRIES` control state;
3. a dedicated Demo trading credential that passes the GET-only least-privilege credential preflight;
4. one exact operator selection of evidence rank and expected symbol plus the exact confirmation phrase;
5. a newly created approval with the existing maximum TTL of 120 seconds;
6. immutable pre-submit authorization lineage and protected-entry provenance;
7. the existing OMS/recovery path that forbids a second ENTRY after submit start.

These capabilities are independent. Approval is not ARM. ARM is not credential authorization. Fixed egress is not an approval. None can substitute for another.

## Protected operational flow

The implemented one-shot flow is:

```text
manual workflow_dispatch
-> protected [self-hosted, bybit-demo] runner / bybit-demo Environment
-> Demo PostgreSQL schema verification
-> GET-only Demo trading credential preflight
-> authoritative latest positive-evidence row + exact fixed history
-> exact Demo instrument resolution
-> connected fixed-egress authenticated preflight
-> read existing v121 ARM; never create ARM here
-> pin exact ARM event_id / kind / armed_until
-> read Demo wallet + existing v122 session-risk ledger
-> create exact <=120 second operator approval
-> canonical approval identity recheck
-> reject already-burned authorization/provenance
-> canonical operator-approved runtime, exactly once
-> Gate 1 / Gate 2 pinned ARM rechecks
-> immutable authorization before any entry network mutation
-> at most one Demo ENTRY submit through OMS-aware api-demo.bybit.com client
-> no automatic resubmit after SUBMIT_STARTED
-> built-in protection reconciliation on canonical success
-> exact GET-only OMS/recovery reconciliation after attempted runtime/entry failure
-> protection restore or deterministic reduce-only flatten when recovery proves execution
-> allowlisted sanitized evidence artifact
```

If any identity/economic check changes the symbol, side, decision, source snapshot, evidence/market rank or approved caps, the entry is rejected rather than rerouted. Ranked fallback is not permitted after approval or after any failed entry attempt.

The operational layer does not select a different instrument, create a new signal, alter strategy logic, alter risk rules, automatically arm v121, or retry an entry.

## Manual dispatch contract

`.github/workflows/bybit-operator-approved-demo-execution.yml` exposes only three execution inputs:

```text
evidence_rank
symbol
confirmation_phrase
```

There are deliberately no workflow inputs for side, quantity, risk, notional, fallback, approval TTL, broker host or `writes_enabled`.

The execution job:

```text
if: workflow_dispatch only
runs-on: [self-hosted, bybit-demo]
environment: bybit-demo
concurrency group: bybit-demo-protected-operational-entry
cancel-in-progress: false
```

`cancel-in-progress: false` is a safety requirement. A GitHub cancellation must not replace a running invocation after ENTRY may have reached `SUBMIT_STARTED` and before reconciliation/protection completes.

Pull-request and push events run qualification only on GitHub-hosted runners. They do not enter the protected execution job.

No schedule exists and there is no autonomous dispatch.

## One-shot runner

`tools/run_bybit_demo_operator_approved_entry.py` composes existing production primitives; it does not implement a second trading engine.

Its order-capable dependency is `OmsAwareBybitDemoStopRatchetClient`. The client remains Demo-only, uses the existing `PostgresBybitEntryOms` plus immutable recovery envelope store, records `SUBMIT_STARTED` before broker POST, and forbids automatic resubmission once submission has started.

The runner loads secrets only from the protected process environment. Command-line or workflow inputs cannot supply credentials, DSNs, broker hosts, sizing or risk parameters.

Mainnet trading credentials are not loaded. Only the pre-existing SHA-256 fingerprint of the mainnet read-only key namespace is used by the GET-only credential-isolation preflight.

Startup and runtime evidence is written atomically to the requested artifact path. Failure evidence is allowlisted and intentionally omits exception messages, DSNs, API keys, secrets and raw broker payloads.

## Exact approval source

`tools.prepare_bybit_demo_operator_approval` and the operational runner share `resolve_bybit_demo_operator_approval_source(...)`.

That read-only helper requires:

- exactly one latest positive-evidence row at the approved rank;
- exact expected symbol match;
- timezone-aware decision identity;
- the same fixed 5-minute historical acquisition window;
- the existing minimum-history validation;
- no bars from another symbol.

This removes two independent ways of reconstructing approval input without changing the signal or strategy.

The standalone preparation command remains non-mutating:

```bash
python -m tools.prepare_bybit_demo_operator_approval \
  --site eu \
  --evidence-rank 1 \
  --symbol BTCUSDT \
  --confirm APPROVE_BYBIT_DEMO_EXECUTION \
  --output artifacts/bybit-demo-approval.json
```

The protected execution workflow creates its short-lived approval only after fixed-egress readiness and the existing v121 ARM have been observed, so a queued environment approval cannot consume most of the 120-second TTL before the protected job starts.

## v121 ARM is existing state, not an action of the runner

See `docs/BYBIT_DEMO_CONTROL_PLANE.md` for the full v121 contract.

Important properties remain:

- no control event means HALT;
- missing, invalid or expired v121 state means HALT;
- ARM max TTL is 300 seconds, default 120 seconds;
- ARM can only be persisted by the existing control-plane path from exact `READY_FOR_MANUAL_OPERATOR_APPROVAL` connected preflight;
- `EXISTING_TRADE_MANAGEMENT_REQUIRED` can never ARM a new entry;
- HALT blocks new non-reduce-only exposure but not protection/reduce-only recovery of an existing trade.

The one-shot entry runner has no ARM writer. It reads the already-existing decision and immediately freezes exact `event_id`, event kind and `armed_until` in `PinnedBybitDemoControlPlane` before creating the trade approval.

Every later new-entry control read must resolve to that same ARM. ARM replacement, drift or expiry blocks the entry rather than silently rebinding the approval to a different control event.

## Demo trading credential gate

See `docs/BYBIT_DEMO_TRADING_CREDENTIAL_PREFLIGHT.md`.

The order-capable worker uses a dedicated Demo key. Before it is composed into the OMS-aware Demo client, the GET-only credential preflight requires the existing least-privilege contract, including concrete IP binding and the required ContractTrade permission set.

Passing this credential gate does not authorize an order. It proves only the credential boundary. Exact operator approval and an already-active pinned v121 ARM are still required.

## Canonical single-writer runtime

`run_protected_bybit_demo_operational_entry` delegates exactly once to `run_operator_approved_bybit_demo_trading_runtime`, which continues to use `run_bybit_demo_trading_runtime`. There is still only one lifecycle engine.

The canonical runtime remains authoritative for:

- lease acquisition;
- durable checkpoint inspection;
- entry-versus-management routing;
- current Demo account sizing under existing risk rules;
- exact single-use approval validation;
- immutable authorization/provenance;
- protection and managed-trade lifecycle;
- restart-safe terminal evidence and session-risk commit;
- prohibition on a replacement entry in the same invocation.

The operational runner passes the existing strategy configuration and existing v122 session ledger. It does not expose risk/sizing switches to the operator.

## Durable pre-submit authorization and at-most-once ENTRY

The immutable authorization remains written as late as possible while still preceding the exchange entry mutation:

```text
canonical trading runtime
-> approved account-sized bridge
-> exact approval / identity guard
-> DurableApprovalLineageBybitDemoClient
   (persist and burn immutable pre-submit authorization)
-> ControlPlaneGuardedBybitDemoClient
   (fresh pinned-ARM check for non-reduce-only entry)
-> OmsAwareBybitDemoStopRatchetClient
   (SUBMIT_STARTED persisted before Demo order/create)
-> api-demo.bybit.com
```

If authorization persistence fails, the broker mutation is never called. If authorization already exists, the deterministic entry identity is burned and cannot be reused for a new submit.

If submission starts and broker outcome is ambiguous, the OMS path performs exact GET-only reconciliation by deterministic `orderLinkId`. There is no same-invocation blind retry and no fallback instrument.

## Mandatory protection reconciliation

Canonical successful protected entry already performs protection reconciliation and persists protected-entry provenance.

If the runtime invocation fails after authorization may have been burned, the operational composer still invokes the dedicated post-attempt reconciliation path. It uses immutable authorization, OMS state, recovery envelope and exact broker truth for the same `orderLinkId`.

That recovery path can only conclude one of the following:

- no execution confirmed;
- canonical protection already reconciled;
- protection restored;
- deterministic reduce-only flatten completed;
- unresolved, fail closed.

It has no ENTRY method and cannot create a replacement position.

## Sanitized evidence

Operational evidence is allowlisted. It may report status, exact immutable record hashes/identities and reconciliation outcome required for audit, but it must not include secrets, DSNs, exception messages or raw broker payloads.

The workflow uploads `artifacts/bybit-demo-operational-entry.json` with `if: always()` so a fail-closed startup/runtime result remains reviewable even when the one-shot command exits non-zero.

## Activation status

The protected one-shot operational composition is implemented in code and covered by PR qualification tests.

That does **not** mean trading has been activated. A real `workflow_dispatch` must remain an explicit operator action in the protected GitHub Environment after all prerequisites are independently verified.

As part of PR #89 implementation/qualification, no protected execution dispatch is required, no Demo order is required, and no mainnet write is permitted.
