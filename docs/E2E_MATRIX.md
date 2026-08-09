# ASTRA Trade Bot — End-to-End Readiness Matrix

This document is the source of truth for product-readiness claims. A capability is not marked PASS merely because code exists; it must have an executable qualification gate and credible evidence. Scores are deliberately conservative: deterministic CI cannot substitute for external broker evidence, GitHub server-side enforcement or elapsed soak time.

## Current product state

| Capability | Current state | Evidence / gate | Score | To reach or retain >= 8/10 |
|---|---|---|---:|---|
| Architecture cohesion | QUALIFIED DETERMINISTICALLY | stable `domain`, `marketdata`, `strategy`, `risk`, `portfolio`, `oms`, `execution`, `application`, `observability`; one composition root for SQLite and PostgreSQL; shared durable OMS, order-mutation, risk and portfolio stores; consolidated `main`; complete regression green | 8.0 | retain >=8 by retiring duplicated versioned domain/runtime models and keeping broker/deployment adapters behind the stable composition boundary |
| Real external integration | BLOCKED BY CREDENTIAL EVIDENCE | real Alpaca Paper REST + WebSocket workflow is implemented and was actually invoked; it failed at the explicit credential gate before network access because repository Alpaca secrets are absent | 4.0 | configure protected Paper credentials; repeated successful REST/WebSocket evidence; controlled Paper mutation drill; disconnect/recovery evidence and broker SLO history |
| Trading logic | STRONG PARTIAL | deterministic long-only validation strategy; no-lookahead next-bar backtester; fees/slippage/drawdown; walk-forward OOS windows; benchmark-relative acceptance framework | 6.0 | real historical market-data adapter; representative multi-regime dataset; parameter/version governance; independent benchmark suite; validated strategy acceptance thresholds |
| Portfolio management | QUALIFIED DETERMINISTICALLY | fee-aware cash/positions, realized/unrealized P&L/equity, idempotent fills, broker reconciliation, stock splits/dividends, SQLite replay/snapshots, PostgreSQL 16 append-only event journal, restart replay, snapshots and concurrent duplicate-event idempotency | 8.0 | retain >=8 with real broker corporate-action/reconciliation evidence; add multi-currency/tax lifecycle only when product scope requires it |
| Risk management | QUALIFIED DETERMINISTICALLY | exposure, kill switch, freshness, execution-quality, loss/drawdown/turnover, liquidity, concentration and volatility guardrails plus immutable SQLite/PostgreSQL risk-decision hash chain wired into the actual paper planning path; PostgreSQL concurrent writers serialize through a locked chain head | 8.0 | retain >=8 through scenario/property testing and production-calibrated thresholds; real Paper evidence remains a separate paper-readiness gate |
| OMS / execution state | QUALIFIED DETERMINISTICALLY | transactional SQLite + PostgreSQL 16 OMS, row locking, append-only events, deterministic client IDs, durable submit and mutation outboxes, monotonic fills, at-most-one submit, at-most-one cancel/replace attempt, `STARTED` before broker mutation, GET-only ambiguity/restart recovery, active-mutation fencing, broker-ID rotation handling, read-only reconciliation and repeated deterministic fault campaigns on canonical `main` | 8.5 | retain >=8 with real broker cancel/replace fault evidence and operational SLO history; scheduled deterministic campaigns support regression confidence but do not replace external soak evidence |
| Release process | STRONG PARTIAL — SIGNED PROVENANCE QUALIFIED | canonical `main`, stable package identity, hash-locked `requirements.lock`, lock freshness verification, Ruff/Bandit/dependency audit, exact Node24-native GitHub Action SHA pins, automated CI supply-chain policy, wheel/sdist + release manifest + SPDX SBOM, signed SLSA build provenance and signed SBOM attestation on canonical `main` | 7.8 | verify/enforce GitHub server-side branch protection and required reviews; execute a signed/version-matched release tag; assign release/rollback ownership; preserve externally verifiable attestations for releases |
| Paper-operation readiness | STRONG PARTIAL | paper safety, trading E2E, immutable risk evidence, PostgreSQL portfolio/OMS durability, at-most-once submit, deterministic cancel/replace recovery, operational SLO and composition-root gates pass on `main`; repeated fault campaign also passes on `main` across SQLite and PostgreSQL | 7.8 | credentialed Alpaca Paper read-only PASS; controlled real Paper mutation/recovery drill; 14–30 day external soak with clean reconciliation/SLO evidence |
| Live trading readiness | BLOCKED | all product qualification paths keep external/live routing disabled; real Paper prerequisites, server-side release governance and independent live-pilot controls are incomplete | 2.0 | every live-critical upstream capability >=8 with external evidence; independent approval; tiny-capital pilot controls; production secrets/KMS; real-time reconciliation; global kill switch |

## Verified evidence snapshots

### Deterministic order-lifecycle fault campaign

Canonical `main` evidence:

- commit: `8dce54a1cf1eccfc7152ef141619f35c8a1e0e81`
- workflow run: `31335919400`
- status: `success`
- PR/push campaign strength: 25 repeated SQLite submit/mutation cycles and 8 repeated PostgreSQL mutation-durability/fencing cycles
- retained artifact: `9044324636`
- artifact digest: `sha256:f0731e880fadc75b3333702c4cb72bc60fd683a3b57cfb39c6efe6ead1fa6869`
- nightly configuration: 100 SQLite cycles and 25 PostgreSQL cycles

This evidence increases deterministic confidence in at-most-once submit/cancel/replace behavior, GET-only ambiguity/restart recovery, monotonic fill adoption and durable mutation fencing. It is not a substitute for a credentialed broker mutation drill or elapsed external soak time.

### Signed release provenance

First qualified signed provenance on canonical `main`:

- commit: `3dc298b4f6d8fba504e560762d101cae6d4070bc`
- workflow run: `31336212403`
- SLSA build-provenance attestation ID: `39704670`
- signed SBOM attestation ID: `39704671`
- retained evidence artifact: `9044407752`
- artifact digest: `sha256:50638e11323333a99e46dca19b39a57901986b5bb6392353232b94824136f2c3`

The attestation job runs only after trusted `main`/tag pushes and verifies SHA-256 checksums after artifact transfer and before signing. Pull-request qualification remains read-only and does not receive OIDC/attestation write privileges.

## Security qualification

The stable product core is required to pass Ruff, Bandit, dependency auditing, compile checks and the complete deterministic pytest regression. During the hardening cycle, `pip-audit` found `PYSEC-2026-3552` in the installed `cryptography 49.0.0` line. The project dependency contract and V108 architecture audit were upgraded to require `cryptography>=50,<51` instead of suppressing the advisory. Current release and product gates install the hash-locked dependency graph, require exact Node24-native GitHub Action commit SHAs for operational workflows, generate signed build/SBOM attestations on trusted `main`/tag pushes, and keep live/production execution flags fail-closed.

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

Dedicated PostgreSQL 16 gates validate OMS row locking, durable outbox behavior, duplicate broker-event idempotency, monotonic fills and append-only OMS events. The order-mutation gate additionally validates migration replay, active-mutation fencing, cancel/replace persistence, broker-ID rotation, concurrent request conflicts and an append-only mutation-event trigger on a real PostgreSQL service. The order-lifecycle fault campaign repeatedly reuses these qualified mutation scenarios against PostgreSQL 16. Separate PostgreSQL 16 gates validate immutable risk evidence with concurrent writers and an append-only portfolio journal with replay, snapshots and concurrent duplicate-event suppression. SQLite remains useful for deterministic/local qualification but is no longer the only durable backend for OMS, order mutations, risk evidence or portfolio state.

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

## Release-governance boundary

Repository-side provenance, lock, dependency, action-pin and release-evidence controls are executable and qualified. GitHub server-side branch protection / ruleset state remains **UNVERIFIED** because the current GitHub integration receives `403 Resource not accessible by integration` from the branch-protection endpoint. Repository files are not treated as proof of server-side review, force-push or required-check enforcement. See `docs/RELEASE_GOVERNANCE.md`.

## Hard live blockers

Live routing must remain disabled while any of these are true:

- external Paper REST/WebSocket qualification has no successful credentialed evidence;
- no controlled stable-OMS cancel/replace mutation/recovery drill exists against the real Paper broker;
- no 14–30 day external Paper soak has completed cleanly;
- there is no representative real historical-data strategy qualification across multiple regimes;
- GitHub server-side branch protection / required-review enforcement and release/rollback ownership remain unverified or incomplete;
- no version-matched release tag has completed the signed provenance path;
- independent live-pilot approval and tiny-capital controls are absent.

## Definition of >=8/10

A score of 8 or above requires implemented behavior, executable automated verification, realistic environment evidence where applicable, documented failure behavior, explicit operational ownership and no known P0/P1 correctness gap in that capability. A deterministic score of 8 in one subsystem never overrides missing external evidence in another subsystem and never authorizes live trading by itself.
