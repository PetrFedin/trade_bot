# ASTRA Trade Bot — End-to-End Readiness Matrix

This document is the source of truth for product-readiness claims. A capability is not marked PASS merely because code exists; it must have an executable qualification gate and evidence on the current branch head.

## Current product state

| Capability | Current state | Evidence / gate | Score | To reach >= 8/10 |
|---|---|---|---:|---|
| Architecture cohesion | PARTIAL | stable `app/domain`, `app/marketdata`, `app/strategy`, `app/risk`, `app/portfolio`, `app/oms`, `app/application`; full regression green | 6.5 | retire duplicated versioned domain models; stable distribution identity; single composition root; PostgreSQL production OMS |
| Real external integration | PARTIAL / NOT YET QUALIFIED | real Alpaca Paper REST + WebSocket workflow exists, but a successful credentialed network run is still required | 4.0 | repeated successful credentialed probes; controlled paper mutation drill; disconnect/recovery evidence; latency/error SLOs |
| Trading logic | PARTIAL | deterministic long-only validation strategy + no-lookahead next-bar backtester with fees/slippage | 4.5 | historical data adapter; benchmark suite; walk-forward/out-of-sample testing; parameter governance; strategy acceptance criteria |
| Portfolio management | PARTIAL | fee-aware position/cash ledger, realized/unrealized P&L, equity, idempotent fills, broker-truth reconciliation | 6.0 | durable snapshots; corporate actions; dividends/fees lifecycle; multi-symbol account reconciliation; broker-ledger recovery |
| Risk management | PARTIAL | order/symbol/gross limits, long-only sell boundary, kill switch, stale price, spread, slippage, market-hours/halt, loss/drawdown/turnover limits | 7.0 | liquidity participation, sector/concentration, volatility sizing, portfolio loss budget, durable risk decisions, independent scenario tests |
| OMS / execution state | PARTIAL | transactional SQLite OMS, append-only events, deterministic client IDs, durable outbox, monotonic cumulative fills, uncertain/reconciliation/manual states | 7.0 | PostgreSQL concurrency model; broker event ingestion; crash/fault injection; cancellation/replacement lifecycle; multi-worker fencing |
| Release process | PARTIAL | draft PR, pinned Actions in new gates, CODEOWNERS, full regression gates | 5.0 | consolidate stacked history onto main; stable package name; dependency lock; signed release/tag; mandatory review/protection; SBOM/security gates |
| Paper-operation readiness | PARTIAL | deterministic trading E2E + durable OMS E2E + legacy regression suite green | 6.0 | real credentialed paper qualification; controlled mutation drill; 14–30 day soak; restart/network/partial-fill fault matrix; alerting/SLOs |
| Live trading readiness | BLOCKED | all current product paths keep live routing disabled | 2.0 | every upstream area >=8; independent approval; tiny-capital pilot controls; real-time reconciliation; global kill switch; production secrets/KMS |

## Current deterministic E2E chain

```text
validated bars
  -> deterministic strategy target
  -> order intent
  -> operational pre-trade risk decision
  -> deterministic client order id
  -> durable OMS create
  -> durable risk approval
  -> durable submit outbox
  -> submit-start / broker acknowledgement state
  -> monotonic fill event(s)
  -> fee-aware portfolio ledger
  -> cash / position / realized P&L / unrealized P&L / equity
  -> read-only broker-truth reconciliation
```

## External Paper chain

The repository now contains a separate real-network qualification workflow. It is intentionally not conflated with deterministic CI.

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

Until that workflow has a successful credentialed run, **real external integration remains unqualified**.

## Hard live blockers

Live routing must remain disabled while any of these are true:

- external Paper network qualification has no current successful evidence;
- no controlled Paper mutation/recovery drill exists for the stable OMS;
- no 14–30 day external Paper soak has completed cleanly;
- unexplained cash or position reconciliation deltas exist;
- PostgreSQL durable OMS/fencing is not qualified for concurrent workers;
- observability and operational SLOs are incomplete;
- release history has not been consolidated and independently reviewed;
- strategy qualification is only deterministic/system-validation level.

## Definition of >=8/10

A score of 8 or above requires: implemented behavior, executable automated verification, realistic environment evidence where applicable, documented failure behavior, explicit operational ownership, and no known P0/P1 correctness gap in that capability.
