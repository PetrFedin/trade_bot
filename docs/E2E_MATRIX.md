# ASTRA Trade Bot — End-to-End Readiness Matrix

This document is the source of truth for product-readiness claims. A capability is not marked PASS merely because code exists; it must have an executable qualification gate and credible evidence. Scores are deliberately conservative: deterministic CI cannot substitute for external broker evidence or elapsed soak time.

## Current product state

| Capability | Current state | Evidence / gate | Score | To reach or retain >= 8/10 |
|---|---|---|---:|---|
| Architecture cohesion | QUALIFIED DETERMINISTICALLY | stable `domain`, `marketdata`, `strategy`, `risk`, `portfolio`, `oms`, `execution`, `application`, `observability`; one composition root for SQLite and PostgreSQL; shared durable OMS, order-mutation, risk and portfolio stores; consolidated `main`; complete regression green | 8.0 | retain >=8 by retiring duplicated versioned domain/runtime models and keeping broker/deployment adapters behind the stable composition boundary |
| Real external integration | BLOCKED BY CREDENTIAL EVIDENCE | real Alpaca Paper REST + WebSocket workflow is implemented and was actually invoked; it failed at the explicit credential gate before network access because repository Alpaca secrets are absent | 4.0 | configure protected Paper credentials; repeated successful REST/WebSocket evidence; controlled Paper mutation drill; disconnect/recovery evidence and broker SLO history |
| Trading logic | STRONG PARTIAL | deterministic long-only validation strategy; no-lookahead next-bar backtester; fees/slippage/drawdown; walk-forward OOS windows; benchmark-relative acceptance framework | 6.0 | real historical market-data adapter; representative multi-regime dataset; parameter/version governance; independent benchmark suite; validated strategy acceptance thresholds |
| Portfolio management | QUALIFIED DETERMINISTICALLY | fee-aware cash/positions, realized/unrealized P&L/equity, idempotent fills, broker reconciliation, stock splits/dividends, SQLite replay/snapshots, PostgreSQL 16 append-only event journal, restart replay, snapshots and concurrent duplicate-event idempotency | 8.0 | retain >=8 with real broker corporate-action/reconciliation evidence; add multi-currency/tax lifecycle only when product scope requires it |
| Risk management | QUALIFIED DETERMINISTICALLY | exposure, kill switch, freshness, execution-quality, loss/drawdown/turnover, liquidity, concentration and volatility guardrails plus immutable SQLite/PostgreSQL risk-decision hash chain wired into the actual paper planning path; PostgreSQL concurrent writers serialize through a locked chain head | 8.0 | retain >=8 through scenario/property testing and production-calibrated thresholds; real Paper evidence remains a separate paper-readiness gate |
| OMS / execution state | QUALIFIED DETERMINISTICALLY | transactional SQLite + PostgreSQL 16 OMS, row locking, append-only events, deterministic client IDs, durable submit and mutation outboxes, monotonic fills, at-most-one submit, at-most-one cancel/replace attempt, `STARTED` before broker mutation, GET-only ambiguity/restart recovery, active-mutation fencing, broker-ID rotation handling and read-only reconciliation | 8.5 | retain >=8 with real broker cancel/replace fault evidence, long-running restart/network campaigns and operational SLO evidence; no live claim follows from this score |
| Release process | STRONG PARTIAL | canonical `main`, stable package identity, hash-locked `requirements.lock`, lock freshness verification, SBOM + release manifest, Ruff/Bandit/dependency audit, exact Node24-native GitHub Action SHA pins and automated CI supply-chain policy | 7.5 | branch protection and required-review enforcement; signed release/tag and externally verifiable provenance/attestation; documented rollback/release ownership |
| Paper-operation readiness | STRONG PARTIAL | paper safety, trading E2E, immutable risk evidence, PostgreSQL portfolio/OMS durability, at-most-once submit, deterministic cancel/replace recovery, operational SLO and composition-root gates pass on `main` | 7.5 | credentialed Alpaca Paper read-only PASS; controlled real Paper mutation/recovery drill; restart/network/partial-fill fault campaign; 14–30 day external soak with clean reconciliation/SLO evidence |
| Live trading readiness | BLOCKED | all product qualification paths keep external/live routing disabled; real Paper prerequisites and release governance are incomplete | 2.0 | every live-critical upstream capability >=8 with external evidence; independent approval; tiny-capital pilot controls; production secrets/KMS; real-time reconciliation; global kill switch |

## Security qualification

The stable product core is required to pass Ruff, Bandit, dependency auditing, compile checks and the complete deterministic pytest regression. During the hardening cycle, `pip-audit` found `PYSEC-2026-3552` in the installed `cryptography 49.0.0` line. The project dependency contract and V108 architecture audit were upgraded to require `cryptography>=50,<51` instead of suppressing the advisory. Current release and product gates install the hash-locked dependency graph, require exact Node24-native GitHub Action commit SHAs for operational workflows, and keep live/production execution flags fail-closed.

## Current deterministic E2E chain

```text
validated market data
  -> data-quality gate
  -> deterministic strategy target
  -> order intent
  -> pre-trade risk evaluation
  -> immutable risk-decision evidence (approved or rejected)
  -> deterministic client order id
  -> durable OMS create
  -> durable risk approval
  -> durable submit outbox
  -> persist SUBMIT_STARTED before network mutation
  -> at-most-one Paper POST
  -> GET-only ambiguity/restart recovery
  -> broker acknowledgement / monotonic fill state
  -> optional durable cancel/replace request
  -> persist mutation STARTED before DELETE/PATCH
  -> at-most-one Paper DELETE/PATCH
  -> GET-only mutation ambiguity/restart recovery
  -> preserve effective limit price / rotated broker order id
  -> append-only portfolio event journal
  -> cash / position / realized P&L / unrealized P&L / income / equity
  -> deterministic portfolio replay / snapshot
  -> read-only broker-truth reconciliation
  -> operational readiness / SLO gate
```

## PostgreSQL qualification evidence

Dedicated PostgreSQL 16 gates validate OMS row locking, durable outbox behavior, duplicate broker-event idempotency, monotonic fills and append-only OMS events. The order-mutation gate additionally validates migration replay, active-mutation fencing, cancel/replace persistence, broker-ID rotation, concurrent request conflicts and an append-only mutation-event trigger on a real PostgreSQL service. Separate PostgreSQL 16 gates validate immutable risk evidence with concurrent writers and an append-only portfolio journal with replay, snapshots and concurrent duplicate-event suppression. SQLite remains useful for deterministic/local qualification but is no longer the only durable backend for OMS, order mutations, risk evidence or portfolio state.

## External Paper chain

The repository contains a separate real-network qualification workflow. It is intentionally not conflated with deterministic CI.

```text
GitHub environment: alpaca-paper
  -> explicit Alpaca Paper credentials
  -> TLS-verified Alpaca Paper REST account/open-order reads
  -> Alpaca Paper trade_updates WebSocket authentication/listen handshake
  -> redacted evidence artifact
  -> assert writes disabled
  -> assert external_order_routing_allowed = false
  -> assert live_trading_allowed = false
```

The workflow has been invoked in GitHub Actions, but the repository currently exposes no required Alpaca secrets to the job. It therefore fails at the credential preflight before making a broker network request. Until a credentialed run succeeds, **real external integration remains unqualified**.

## Hard live blockers

Live routing must remain disabled while any of these are true:

- external Paper REST/WebSocket qualification has no successful credentialed evidence;
- no controlled stable-OMS cancel/replace mutation/recovery drill exists against the real Paper broker;
- no 14–30 day external Paper soak has completed cleanly;
- there is no representative real historical-data strategy qualification across multiple regimes;
- branch protection, signed release/provenance and release-ownership controls are incomplete;
- independent live-pilot approval and tiny-capital controls are absent.

## Definition of >=8/10

A score of 8 or above requires implemented behavior, executable automated verification, realistic environment evidence where applicable, documented failure behavior, explicit operational ownership and no known P0/P1 correctness gap in that capability. A deterministic score of 8 in one subsystem never overrides missing external evidence in another subsystem and never authorizes live trading by itself.
