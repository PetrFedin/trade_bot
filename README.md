# ASTRA Trade Bot

ASTRA is being consolidated into a **single-user production trading product** with one canonical execution/risk/accounting lifecycle. The current integration branch is `agent/trading-quality-profit-protection`; the active integration PR is **#41**.

PR #41 is an integration/incubator snapshot, not a production release unit. It is intentionally kept draft while the release-critical gates, production operating layer and independent trading evidence are completed.

## Product direction

The target is not an MVP and not a demo-only bot. The target is a continuously operable trading product with:

- one canonical trade intent and risk boundary;
- one authoritative OMS/accounting model;
- broker adapters behind that model;
- durable PostgreSQL state;
- deterministic reconciliation and fail-closed recovery;
- 24/7 runtime, observability, operator controls and release governance;
- research isolated from production strategy activation.

The current Bybit qualification path still uses demo/shadow environments because real-money routing must remain independently gated. Environment qualification is not the product definition.

## Current trading contract

The active crypto research contract continues to evaluate **both LONG and SHORT** opportunities. Historical SHORT weakness is treated as a prospective research hypothesis only; SHORT is not disabled post-hoc.

New entries require at least **$20 modeled net edge after configured costs**. Pending entries are revalidated against fresh executable pricing and may only shrink in quantity. Session risk, correlation diversification, liquidation safety and next-open execution-risk guards remain fixed. The open-ended runner remains conditional on the frozen 1.5x excess-edge admission gate; it is not weakened to manufacture trade frequency.

No part of the project claims every trade can close profitably. The objective is positive net expectancy, retained profit when available, and bounded downside after actual fees, funding and execution effects.

## Canonical trading lifecycle

```text
market data
  -> strategy / candidate generation
  -> position selection
  -> canonical risk checks
  -> fresh-price / execution-economics recheck
  -> durable order intent / OMS
  -> broker order / execution
  -> broker-truth position reconciliation
  -> protection / ongoing management
  -> terminal execution reconciliation
  -> account closed-PnL reconciliation
  -> funding reconciliation
  -> fully reconciled all-in PnL
  -> immutable trade attribution / evidence
```

A broker ACK is never sufficient proof of a valid protected position or a completed close. Unknown mutation state, missing protection, reconciliation gaps, unexplained account deltas and unsafe capability boundaries fail closed.

## What is already strong

The repository already contains:

- stable domain, market-data, strategy, risk, portfolio, OMS, application and observability layers;
- transactional SQLite and PostgreSQL OMS paths with append-only events, deterministic client IDs, outbox/fencing and reconciliation;
- Bybit strategy selection, fresh-price recheck, account sizing, protection, liquidation veto, trade monitoring, max-hold exit and emergency reduce-only flattening;
- actual fee/funding-aware terminal accounting;
- MFE/MAE, capture, giveback, realized R, slippage and edge-realization diagnostics;
- immutable entry provenance and terminal evidence with checksum/conflict/idempotency contracts;
- anti-overfit research gates, walk-forward evidence and prospective directional validation;
- extensive deterministic CI and release provenance/security controls.

## Current production blockers

Mainnet/live order routing remains disabled. The principal blockers are operational and evidentiary rather than feature-count related:

- exact release head must be fully green across global regression and release-critical Bybit gates;
- GitHub `main` server-side branch protection / required review enforcement is still disabled;
- PR #41 is too large to be used directly as a production release unit and must be consolidated into reviewable release changes;
- authoritative Bybit runtime state still needs convergence onto the production PostgreSQL OMS/accounting model;
- canonical 24/7 Bybit service startup/recovery/reconciliation path is not yet the sole runtime entrypoint;
- production private-stream + REST-truth reconciliation, operational telemetry, alerting and operator controls need completion;
- production configuration/secrets boundary and deployment/recovery runbooks need completion;
- strategy edge still requires substantially more independent forward evidence, multi-regime coverage and cost/latency/liquidity stress;
- historical funding/mark evidence remains incomplete;
- a tiny-capital real-money pilot must be a separate explicit release decision after all prior gates pass.

## Release and readiness rules

Code existence never equals readiness. A capability is considered qualified only when there is an executable gate and credible evidence for the exact revision/environment being promoted.

See:

- `docs/E2E_MATRIX.md` — source of truth for readiness claims, evidence and hard blockers;
- `docs/RELEASE_PROCESS.md` — release qualification and promotion rules;
- `docs/RELEASE_GOVERNANCE.md` — ownership and repository governance evidence.

No README statement overrides the E2E matrix, broker reconciliation result, risk state or live/mainnet safety flags.
