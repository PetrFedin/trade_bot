# ASTRA Trade Bot

ASTRA is being consolidated from a sequence of safety/control-plane schemas into a coherent paper-trading product. The current product branch is `agent/schema108-paper-core-foundation` and the active integration PR is #12.

## What exists now

The repository contains:

- stable trading domain models for bars, targets, order intents and fills;
- deterministic long-only strategy logic used for system validation;
- no-lookahead historical backtesting with next-bar execution, fees and adverse slippage;
- fail-closed market-data quality checks;
- pre-trade risk limits for order/symbol/gross exposure, stale prices, market close/halt, spread, slippage, daily loss, drawdown and turnover;
- a fee-aware long-only portfolio ledger with realized/unrealized P&L and equity;
- a transactional durable OMS with append-only events, deterministic client IDs, durable submit outbox, monotonic cumulative fills and explicit uncertain/reconciliation/manual states;
- read-only broker-truth order and portfolio reconciliation;
- Alpaca Paper REST/WebSocket adapters and a separate real-network read-only qualification workflow;
- the historical Schema 99–108 safety, deployment and signing-control layers.

## Deterministic product E2E

```text
market data
  -> data quality
  -> strategy target
  -> order intent
  -> pre-trade risk
  -> durable OMS
  -> durable outbox
  -> broker acknowledgement/fill state
  -> portfolio cash/position/P&L
  -> broker-truth reconciliation
```

The deterministic product gates run alongside the historical stacked regression suite in GitHub Actions.

## External Paper qualification

Mock/deterministic CI is deliberately separated from real external verification. The workflow `.github/workflows/external-alpaca-paper-readonly.yml` uses the protected `alpaca-paper` GitHub environment and performs real Alpaca Paper REST reads plus a `trade_updates` WebSocket handshake with broker writes disabled.

A real external capability is **not considered qualified until that credentialed workflow has completed successfully and produced current evidence**.

## Safety state

```text
paper_order_writes_enabled = false by default
external_order_routing_allowed = false
live_trading_allowed = false
```

The project is not yet live-trading ready. Current hard blockers include a controlled stable-OMS Paper mutation drill, long-duration external Paper soak, PostgreSQL OMS qualification for concurrent workers, full account reconciliation/fault injection, production observability/SLOs, release consolidation and stronger strategy qualification.

## Readiness source of truth

See:

- `docs/E2E_MATRIX.md` — honest capability scores, evidence and blockers;
- `docs/RELEASE_PROCESS.md` — release qualification policy;
- `RELEASE_IDENTITY_V108.json` and V108 documents — historical Schema 108 signing/control-plane qualification.

No README statement overrides the E2E matrix or live safety flags.
