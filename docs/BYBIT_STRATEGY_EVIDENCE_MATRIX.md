# Bybit strategy evidence matrix

## Purpose

This layer turns the existing historical replay diagnostics into one point-in-time evidence row per
closed trade and then aggregates those rows into the requested decision matrix:

```text
coin
x LONG / SHORT
x market regime
x OI regime
x crowding regime
x prior funding regime
x stress regime
-> PnL / PF / win rate / MFE / MAE / drawdown / sample size
```

It does not change the qualified strategy, tune entry thresholds, promote a strategy or activate
order routing.

## Point-in-time evidence row

A row is joined by exact trade identity:

```text
(symbol, side, decision_time, entry_time, exit_time)
```

The pre-entry features are reconstructed only from information available at or before the original
`decision_time`:

- fixed-strategy price/EMA/ATR signal state;
- volatility regime;
- EMA trend strength in ATR units;
- breakout state;
- turnover regime;
- latest historical open interest and OI delta;
- long/short account-ratio crowding;
- latest already-settled funding rate.

Realized PnL, MFE and MAE are outcome columns used only for retrospective grouping. They are never
fed back into the historical signal reconstruction.

## Stress context

Bybit market-wide historical liquidation events are **not reconstructed** in this change unit. The
report therefore exposes:

```text
historical_market_wide_liquidation_events_available=false
source=NOT_RECONSTRUCTED
```

Instead, a transparent stress proxy is calculated from five independently visible conditions:

1. the accepted trade was already in `VOL_HIGH_NORMAL`;
2. the completed decision bar moved at least half of the strategy's maximum one-bar ATR limit;
3. absolute OI delta is at least 1%;
4. account-ratio crowding is `LONG_HEAVY` or `SHORT_HEAVY` under the existing 55/45 rule;
5. the crowded side is also the funding-paying side based on the latest already-settled funding
   sign.

Default descriptive states are:

- score 0 -> `STRESS_CALM`;
- score 1-2 -> `STRESS_ELEVATED`;
- score 3-5 -> `STRESS_HIGH`.

If the point-in-time derivatives context is incomplete, the state is `STRESS_UNKNOWN` and missing
reasons are retained. No missing OI/crowding/funding value is guessed.

These thresholds are diagnostic policy, not a newly promoted strategy rule.

## Execution economics

For every persisted replay `ENTRY` event, the layer reconstructs:

- executed replay entry price;
- replay quantity and notional;
- expected net edge saved by the strategy;
- minimum required net edge;
- risk budget;
- modeled round-trip fee + slippage cost using the fixed strategy assumptions;
- modeled cost / expected edge;
- expected edge / risk budget.

The execution-cost amount is a strategy-model estimate. It is not a broker-fee ledger fact and does
not reconstruct historical order-book depth.

## Matrix metrics

Every full composite cell reports:

- trade count and sample sufficiency;
- wins / losses / win rate;
- total and average net PnL;
- profit factor;
- average MFE in R;
- average MAE in R;
- maximum drawdown of the chronologically ordered trade-PnL sequence in USDT;
- average expected pre-entry edge;
- average modeled round-trip execution cost;
- average modeled cost / expected edge;
- average expected edge / risk.

The default minimum cell size is five trades, inherited from the historical repeated-pattern
qualification minimum. Small cells remain visible but are explicitly `sample_sufficient=false`.

The artifact also emits broader `symbol|side` and stress-regime summaries so sparse full-composite
cells can be interpreted without pretending that one or two observations establish an edge.

## Research pipeline integration

`tools/research_bybit_dynamic_top10.py` now emits:

```text
strategy_evidence_matrix
```

alongside:

- current dynamic Bybit Top-10 universe;
- full hourly history profile;
- recent 5-minute execution-grade replay history;
- walk-forward strategy qualification;
- historical OI/account-ratio/funding context;
- reconstructed public-history funding PnL in USDT.

Known evidence gaps remain explicit:

- broker funding ledger is not yet reconciled against the isolated read-only mainnet account;
- historical market-wide liquidation events are not reconstructed;
- historical order-book depth is not available from standard V5 kline history.

## Interpretation boundary

The matrix answers: "under which historically observed combinations did this fixed strategy perform
better or worse?"

It does **not** establish that a condition caused the result, does not guarantee future profit and
does not turn a research cell into a live trading instruction. The artifact remains:

```text
parameter_retuning_performed=false
strategy_selection_allowed=false
strategy_promotion_allowed=false
demo_activation_allowed=false
live_activation_allowed=false
bybit_live_order_routing_allowed=false
causal_claim_allowed=false
predictive_guarantee_allowed=false
```
