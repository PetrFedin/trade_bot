# ASTRA Trade Bot — End-to-End Readiness Matrix

This document is the current source of truth for product-readiness claims. Code existence is never a PASS by itself: the exact revision must have executable qualification and credible evidence. Deterministic CI does not replace broker evidence, elapsed soak time, server-side repository governance or independent strategy evidence.

## Scope and release boundary

There are three boundaries that must not be conflated:

- `main` is the stable product baseline and remains the eventual protected production integration branch.
- `agent/trading-quality-profit-protection` / PR #41 is the frozen Bybit integration/incubator snapshot at `69fcf5fca89d16b5ac1ae4bf11d79062b01655b8`. It is intentionally large and **must not be merged directly as a production release unit**.
- `agent/bybit-production-consolidation` / PR #43 is the first bounded P1 production-consolidation stack on PR #41. Its qualified head `4cb3c011a83ba18e0f6063f05ede8549c081367e` wires durable operator authority into the canonical supervisor and CLI while keeping mainnet disabled.

The last fully qualified code checkpoint before the current operational-health change is `4cb3c011a83ba18e0f6063f05ede8549c081367e`. GitHub Actions associated with that exact head completed successfully for `stable-core-quality`, `postgres-oms-e2e`, `trading-core-e2e`, `risk-marketdata`, `durable-oms-e2e`, `product-composition`, `operational-readiness`, `strategy-qualification`, `paper-executor-e2e` and `schema99-external-paper-roundtrip`.

The current stacked branch `agent/bybit-operational-health` extends that qualified boundary. Its code must be requalified on its own exact head before any PASS claim is advanced.

This evidence qualifies deterministic behavior only. It does **not** authorize live/mainnet routing or real capital.

## Current product-readiness summary

| Capability | Current state | Evidence / interpretation | Remaining gate |
|---|---|---|---|
| Research / backtesting | STRONG PARTIAL | no-lookahead research framework, fixed strategy contracts, cost-aware replay, walk-forward and prospective experiments | more months, more trades, multi-regime forward evidence, full funding/mark history, confidence intervals and stress |
| Current crypto strategy edge | RESEARCH CANDIDATE ONLY | 28d `CONDITIONAL_COMBINED_RISK` about +34.62 USDT, PF about 1.307, 3/4 positive folds, worst fold DD about 3.55%, no risk-budget breaches | substantially larger independent forward sample and regime coverage before capital promotion |
| LONG / SHORT policy | ACTIVE BOTH SIDES | LONG historical performance is stronger and SHORT historical performance is weak, but that is a hypothesis only; no post-hoc SHORT disable is allowed | finish the predeclared prospective directional experiment before any policy change |
| Runner | ARCHITECTURALLY READY, EDGE UNPROVEN | frozen 1.5x admission gate and runner-management lifecycle exist; recent opportunity evidence produced no runner admissions | prove incremental portfolio expectancy prospectively; do not weaken gate for frequency |
| Position selection | SHADOW / EVIDENCE INSUFFICIENT | economic ranking exists, but recent audit had no simultaneously comparable executable candidates | accumulate real comparable candidate moments before changing production ordering |
| Canonical service runtime | STRONG PARTIAL | one packaged Bybit product CLI/composition/supervisor exists with config validation, startup reconciliation, durable operator gating, continuous loop and graceful shutdown | make this the sole operational Bybit path and finish health/fault/soak qualification |
| Canonical OMS convergence | STRONG PARTIAL | new Bybit ENTRY now uses canonical PostgreSQL OMS state transitions, durable submit claim, deterministic `orderLinkId`, at-most-once POST and GET-only ambiguity recovery | converge remaining protection/close/execution accounting events onto the canonical lifecycle without duplicating risk logic |
| PostgreSQL authority | STRONG PARTIAL | runtime lease/fencing, excursion checkpoint, entry provenance, terminal evidence, session-risk state, canonical entry OMS and operator-control state are durable in PostgreSQL | finish remaining reconciliation/accounting/operational authority and backup/restore qualification |
| Distributed fencing | QUALIFIED DETERMINISTICALLY | PostgreSQL runtime lease/fencing and canonical OMS locking are covered by real PostgreSQL E2E | prove restart/failover behavior in deployment fault campaigns |
| Startup recovery | STRONG PARTIAL | startup broker truth reconciliation exists; unresolved Bybit ENTRY submissions block new entries; ambiguous ENTRY is recovered by GET using `orderLinkId` | complete lifecycle adoption for filled/partial/cancelled recovered orders and prove crash/restart campaigns end-to-end |
| ENTRY mutation uncertainty | QUALIFIED DETERMINISTICALLY | `SUBMIT_STARTED` is durable before POST; no automatic resubmit; broker read can adopt ACK; unresolved truth becomes durable `UNCERTAIN`; startup remains blocked | operational reconciliation workflow for all non-terminal uncertainty and live broker evidence |
| Private WS + REST truth | STRONG PARTIAL | private stream monitor exists for reaction/health; reconnect/event boundaries force REST reconciliation; REST remains broker truth | sequence/gap/clock/rate-limit/network-partition fault evidence and sustained external soak |
| Operator control plane | QUALIFIED DETERMINISTICALLY | PostgreSQL `RUNNING / PAUSED / READ_ONLY / KILLED`, append-only action history, actor/reason and separate `clear-kill` are wired into the canonical supervisor and CLI; active-trade safety management remains available while new entries are blocked | deployment fault/soak evidence and external operational runbook rehearsal |
| Operational observability | PARTIAL | structured JSON logging, REST health, market-data age, private-stream health, reconciliation age/state, unresolved ENTRY count, durable kill state and wallet drawdown are wired; current stacked change adds UTC-day fully reconciled all-in realized PnL without fabricating missing values | qualify the current exact head; add authoritative cash reconciliation, metrics/tracing, alerting/dashboard and fault-campaign SLO evidence |
| Configuration | STRONG PARTIAL | canonical Bybit config schema validates environment, broker, endpoints, DB and write/mainnet capability boundaries | finish production secrets/KMS contract and deployment-specific validation |
| Secrets / production key boundary | BLOCKED FOR PRODUCTION | live/mainnet routing remains fail-closed | separate Bybit subaccount, trade-only key, withdrawals disabled, IP allowlist, rotation and secret-manager/KMS evidence |
| Release artifacts / supply chain | STRONG PARTIAL | locked dependencies, audit, pinned Actions policy, build, SBOM, manifest, provenance/attestation gates exist | protected main, required reviews/checks, version-matched signed production tag and bounded release PRs |
| GitHub main governance | BLOCKED | live branch summary reports `protected=false` and required status-check enforcement off | enable server-side branch protection, PR-only merge, required checks/reviews, no force-push and independently verify |
| PR / change-unit governance | STRONG PARTIAL | PR #41 is frozen; subsequent P1 work is proceeding as bounded stacked branches/PRs rather than continuing the integration snapshot | keep production changes reviewable and enforce the same discipline server-side |
| Historical all-in accounting evidence | PARTIAL / EXTERNAL DATA BLOCKER | actual demo lifecycle includes fees, closed-PnL and funding reconciliation; workflows fail closed when external historical funding/mark evidence is unavailable | obtain defensible funding-time mark coverage and repeat all-in historical qualification |
| Tiny-capital pilot readiness | NOT READY | deterministic execution quality is improving, but P0/P1 operations and independent edge evidence are incomplete | all production-operation gates plus separate pilot approval |
| Unattended real-money production | BLOCKED | mainnet capability remains disabled by contract | Definition of Done below plus independent live release decision |

## Canonical Bybit runtime contract

The target runtime is one product path:

```text
market data
  -> strategy / candidate generation (LONG and SHORT)
  -> selection
  -> canonical risk boundary
  -> fresh executable-price / account-economics recheck
  -> canonical durable order intent / OMS
  -> Bybit broker adapter
  -> broker execution / private-stream reaction
  -> REST broker-truth reconciliation
  -> portfolio / protection / management
  -> terminal execution reconciliation
  -> account closed-PnL reconciliation
  -> funding reconciliation
  -> fully reconciled all-in PnL
  -> immutable attribution / evidence
```

Safety invariants:

- unsafe capability rejection is never downgraded to an ordinary retryable diagnostics error;
- unknown mutation state blocks new money-moving actions;
- quantity may shrink after fresh-price recheck but may not grow beyond approved risk;
- broker ACK is not proof of a protected position or completed close;
- an ambiguous ENTRY is never blindly resubmitted after durable `SUBMIT_STARTED`;
- REST is authoritative broker truth; private WebSocket data is a reaction/health layer;
- pause/read-only/kill must block new entries without abandoning protection/management of an already open trade;
- actual fees and funding are part of final all-in PnL;
- operational health may publish zero only when an authoritative source proves zero; unavailable measurements remain unavailable and fail closed;
- research candidates cannot automatically change production strategy policy;
- live/mainnet routing remains disabled during the current consolidation phase.

## Current strategy evidence boundary

The active research source remains `CONDITIONAL_COMBINED_RISK` and continues to evaluate both LONG and SHORT opportunities.

Current evidence is useful but insufficient for production capital:

- roughly 28 days of four non-overlapping 7d cold-start walk-forward folds;
- aggregate PnL about +34.62 USDT;
- PF about 1.307;
- 3/4 positive folds;
- worst fold drawdown about 3.55%;
- no observed risk-budget breaches in that qualification;
- LONG positive while SHORT negative in the observed sample.

The LONG-only idea is a **prospective shadow experiment**, not an active policy. Its predeclared validation window is 2026-08-17 through 2026-09-13. Until that window is complete and acceptance gates are met, SHORT remains enabled in the active research contract.

No result here claims that every trade can or should close profitably. The objective is positive net expectancy after costs, retained favorable excursion when available and bounded downside.

## Operational accounting semantics

The readiness field `daily_pnl` is not inferred from a process-start balance and is not equated to raw account-equity movement. On the current Bybit product path it is defined as the sum of **fully reconciled all-in terminal trade outcomes** whose broker `updatedTime` falls within the current UTC day up to the observation time. Those outcomes have already passed fill-level execution reconciliation, account closed-PnL reconciliation and funding reconciliation before entering the durable PostgreSQL session ledger.

This is intentionally a realized all-in daily loss gate. Open-position deterioration is covered separately by wallet-backed high-water drawdown. The `cash_mismatch` readiness input remains unavailable until the product has a defensible durable cash baseline and broker/local cash bridge; it must not be populated with a synthetic zero.

## CI / evidence tiers

### Fast PR gate

- Ruff / static quality;
- focused unit tests;
- affected integration tests;
- configuration and security basics.

### Full PR gate

- complete deterministic regression;
- PostgreSQL durability/fencing/recovery tests;
- Bybit lifecycle/protection/accounting integration;
- stable-core security/quality.

### Scheduled evidence

- 14d/28d and later multi-cycle walk-forward;
- prospective directional evidence;
- funding/mark acquisition;
- restart/network/latency/liquidity fault campaigns;
- forward qualification soak.

### Release gate

- exact-head full regression;
- PostgreSQL state/recovery qualification;
- security/dependency lock;
- build + SBOM + manifest + provenance;
- exact strategy-policy hashes;
- migration manifest;
- server-side branch/review governance verification;
- immutable version-matched release tag.

## P0 status

| P0 item | Status |
|---|---|
| attributed-runtime safety failures | CLOSED on current integration line |
| exact-head stable/full regression | GREEN at `4cb3c011a83ba18e0f6063f05ede8549c081367e`; every later revision must requalify |
| unsafe capability vs retryable diagnostics separation | CLOSED for the attributed-runtime defect that triggered the audit |
| managed/full Bybit CI coverage | ACTIVE and green on qualified checkpoints |
| freeze PR #41 as integration snapshot | CLOSED at `69fcf5fca89d16b5ac1ae4bf11d79062b01655b8` |
| protect `main` with server-side required checks/reviews | BLOCKED — protection currently disabled and cannot be claimed from CI alone |
| README / Release / E2E drift | E2E advanced to the current stacked production-consolidation boundary; README/release references should continue to be checked before promotion |
| enable mainnet | FORBIDDEN / NOT PART OF P0 |

## P1 status

| P1 item | Status |
|---|---|
| one canonical Bybit service entrypoint | IMPLEMENTED, needs final operational qualification |
| Bybit lifecycle -> stable OMS | ENTRY integrated; remaining lifecycle convergence incomplete |
| authoritative PostgreSQL state | MAJOR PORTION IMPLEMENTED; remaining operational/accounting convergence incomplete |
| distributed lease + fencing | IMPLEMENTED / real-Postgres qualified |
| startup reconciliation | IMPLEMENTED / recovery coverage expanding |
| private WS + REST truth | IMPLEMENTED foundation / external fault evidence incomplete |
| central config schema | IMPLEMENTED |
| `.env.example` | verify/retain as configuration documentation; it is not a production secret store |
| secrets/KMS | NOT COMPLETE |
| structured JSON logging | IMPLEMENTED foundation |
| authoritative readiness inputs | PARTIAL — daily realized all-in PnL added on current stack; cash bridge still unavailable/fail-closed |
| Prometheus/OpenTelemetry | NOT COMPLETE |
| alerting | NOT COMPLETE |
| operator control plane | runtime + CLI IMPLEMENTED / deterministic CI green at `4cb3c011...` |
| backup + restore | NOT QUALIFIED |
| crash/restart/network fault campaign | PARTIAL deterministic coverage; full product campaign incomplete |

## P2 evidence still required

Before real capital, the project still needs:

- multiple months of independent forward qualification;
- materially larger closed-trade sample;
- completion of the predeclared LONG-only hypothesis test without disabling SHORT early;
- regime diagnostics before any regime-aware strategy changes;
- signal -> quote -> order ACK -> fill latency/slippage attribution;
- real comparable-position selection evidence;
- defensible historical funding/mark coverage;
- multi-cycle walk-forward;
- bootstrap/confidence intervals;
- transaction-cost, latency, slippage and liquidity stress;
- predeclared strategy kill criteria;
- shadow challenger evidence separated from production activation.

## Definition of Done before production-ready claim

All of the following must be true at the same time:

1. exact release head is 100% green;
2. `main` is server-side protected with required checks and independent review enforcement;
3. Bybit authoritative state is durable in PostgreSQL;
4. distributed fencing is proven;
5. restart recovery is proven end-to-end;
6. private WS reaction + REST broker-truth reconciliation is proven under disconnect/gap/fault cases;
7. production secrets use least privilege and secret-manager/KMS controls;
8. observability, metrics/tracing and alerts are running;
9. operator pause/read-only/kill controls are wired and qualified;
10. no unexplained account/position deltas remain;
11. forward qualification soak is at least 30 clean days;
12. forward trade sample is sufficient for the predeclared statistical gates;
13. strategy passes prospective evidence gates without post-hoc rule changes;
14. all-in PnL uses actual fees and funding;
15. a tiny-capital pilot is approved as a separate release decision.

Until every applicable gate is satisfied, `live_mainnet_order_routing_allowed=false` remains the required product posture.
