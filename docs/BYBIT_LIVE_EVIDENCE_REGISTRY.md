# Bybit live evidence-ranked opportunity registry

## Purpose

The live evidence registry separates three different concepts that must not be collapsed into one
score:

1. **market rank** — current public Bybit market suitability from the qualified Top-10/Top-50
   universe selector;
2. **fixed-strategy signal** — whether the already-qualified strategy currently has a LONG or SHORT
   signal on completed bars and can build its unchanged trade plan;
3. **historical evidence rank** — how the exact current state maps to the latest qualified historical
   evidence matrix.

A liquid coin is not automatically a trade. A current signal is not automatically a qualified
opportunity. A historically positive cell is not a guarantee of future profit.

## Exact matching contract

For a candidate to receive `QUALIFIED_POSITIVE_EVIDENCE`, all of the following must exist:

- the symbol is in the current qualified market registry;
- the fixed production strategy produces a current signal on completed 5-minute bars;
- the unchanged fixed strategy can build an eligible trade plan;
- point-in-time OI, account-ratio crowding and prior funding context is complete at the signal's
  `decision_time`;
- the current market/stress state maps to an **exact** historical evidence cell;
- the exact cell is `sample_sufficient=true`;
- historical total PnL is positive;
- historical average PnL is positive;
- historical profit factor is above 1, or the cell had no losing trades and positive PnL.

A sample-sufficient exact cell that does not satisfy every positive-history condition is retained as
`QUALIFIED_MIXED_EVIDENCE`. Missing or small cells remain visible but are explicitly unqualified.

## Qualification states

The registry preserves candidates instead of hiding inconvenient evidence:

```text
QUALIFIED_POSITIVE_EVIDENCE
QUALIFIED_MIXED_EVIDENCE
NO_SAMPLE_SUFFICIENT_EXACT_CELL
DERIVATIVES_CONTEXT_INCOMPLETE
TRADE_PLAN_REJECTED
NO_FIXED_STRATEGY_SIGNAL
MARKET_HISTORY_UNAVAILABLE
```

This makes absence of evidence distinguishable from negative/mixed evidence.

## Ranking semantics

Ranking is deliberately lexicographic rather than a newly fitted weighted score. The order is:

1. qualification state;
2. historical profit factor;
3. historical average PnL;
4. historical sample size;
5. current fixed-strategy signal quality;
6. current market rank;
7. symbol as a deterministic tie-breaker.

Therefore a coin ranked lower on pure liquidity can move above a more liquid coin when its exact
current state has stronger sample-sufficient historical evidence. No parameter is fitted to maximize
this ranking and the production strategy is not retuned.

## Same regime definitions in research and current ranking

The historical evidence matrix now persists `turnover_reference_usdt`, the median historical
turnover reference that produced `TURNOVER_HIGH` / `TURNOVER_LOW` in research. The current ranker
uses that exact reference.

The shared classifiers are also reused for:

- low/mid/high normal ATR regime;
- EMA trend strength in ATR units;
- breakout confirmed/pullback;
- turnover high/low;
- OI rising/falling/flat;
- 55/45 account-ratio crowding;
- prior settled funding sign;
- the five-feature stress policy.

This prevents research/live drift from two similar but different formulas.

## No lookahead

For every current signal the derivatives join takes only observations at or before `decision_time`.
The previous OI point must be strictly earlier than the selected current OI point. Future OI,
account-ratio and funding observations are ignored even if present in the fetched history window.

## Refresh design

The intended production cadence is every 10 minutes:

```text
public Bybit Top-10/Top-50 snapshot
-> latest completed 5m bars
-> fixed-strategy signal evaluation
-> derivatives history only for symbols with a current signal
-> exact evidence-cell lookup
-> ranked opportunity snapshot
-> append-only PostgreSQL
```

Historical full-period research is **not** rerun every 10 minutes. The evidence matrix is a separate,
versioned snapshot that can be refreshed on a slower research cadence. The live loop only consumes
the latest qualified evidence snapshot. This keeps the loop fast and prevents a research refresh
from silently changing the trading decision path mid-cycle.

The derivatives optimization is intentional: Top-50 candidates without a current fixed-strategy
signal do not trigger OI/account-ratio/funding history requests.

## PostgreSQL source of truth

Migration:

```text
migrations/v111/001_bybit_live_evidence_registry.sql
```

Tables:

- `astra_bybit_strategy_evidence_snapshot_v111` — immutable versioned historical evidence matrix;
- `astra_bybit_live_opportunity_snapshot_v111` — immutable ranked current snapshot linked to both
  the market-universe snapshot and evidence snapshot;
- `astra_bybit_live_opportunity_candidate_v111` — ranked candidate rows.

All three tables are append-only. Database checks force the non-trading safety boundary.

## Operational command

Using a qualified research artifact:

```bash
python -m tools.refresh_bybit_live_evidence_registry \
  --site eu \
  --registry-limit 50 \
  --equity 1000 \
  --equity-source RESEARCH_REFERENCE \
  --evidence-json artifacts/bybit_dynamic_top10.json \
  --output artifacts/bybit-live-evidence-registry.json
```

Using the latest evidence snapshot already stored in PostgreSQL:

```bash
export BYBIT_OPPORTUNITY_DATABASE_DSN='postgresql://...'
python -m tools.refresh_bybit_live_evidence_registry \
  --site eu \
  --registry-limit 50 \
  --equity 1000 \
  --equity-source RESEARCH_REFERENCE \
  --output artifacts/bybit-live-evidence-registry.json \
  --persist-postgres
```

Database secrets are read from the environment and are never written into the snapshot or CLI
summary.

## Equity source

`RESEARCH_REFERENCE` is intentionally supported for qualification and demo research. A future
integration may feed equity from the isolated read-only mainnet account boundary, but this registry
does not obtain permission to submit, cancel or amend real orders by doing so.

## Safety boundary

Every candidate and snapshot remains:

```text
operator_review_required=true
trade_actionable=false
strategy_parameters_changed=false
strategy_promotion_allowed=false
demo_activation_allowed=false
live_activation_allowed=false
bybit_live_order_routing_allowed=false
causal_claim_allowed=false
predictive_guarantee_allowed=false
```

The registry is evidence for operator review and later demo-qualified decision logic. It is not an
autonomous real-money execution surface.
