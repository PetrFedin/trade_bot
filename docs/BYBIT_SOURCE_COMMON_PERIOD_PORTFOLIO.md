# Bybit source-common shared-capital Top-10 replay

This layer answers a different question from the per-symbol evidence matrix.

The per-symbol source-common evidence matrix asks where the unchanged fixed strategy historically had an observed edge for each coin and state. The portfolio replay asks what the same strategy would have actually done when the current dynamic Top-10 symbols competed for one equity pool and the existing maximum of two concurrent positions.

## Historical interval

For every current dynamic Top-10 symbol the v114 derivatives coverage exposes the earliest instant at which price, open interest, account-ratio crowding and funding can all be supported. The portfolio start is the latest of those ten per-symbol starts:

```text
portfolio_common_start = max(symbol_source_common_start for current Top-10)
```

The end is the first UTC instant after the latest completed v113/v114 archive day. Every selected symbol must contain every exact five-minute price bucket in this synchronized interval. A single missing bar fails the replay; timestamps are not silently intersected and gaps are not interpolated away.

For instruments older than Bybit's documented account-ratio floor, the period remains a maximum source-available common period. It is not relabeled as full instrument lifetime.

## Historical selection contract

The portfolio replay calls the existing canonical multi-symbol `replay_open_ended_crypto_runner` with the unchanged qualified fixed configuration:

- 9/21 EMA, fixed momentum/breakout/ATR filters;
- 1% risk fraction per accepted plan;
- maximum two concurrent positions;
- existing fee and slippage assumptions;
- existing protection and conditional 1.50x runner-admission contract;
- completed-bar decision followed by next-bar-open execution.

At every completed five-minute bar, the canonical runner ranks only signals available at that moment. When more valid plans exist than available slots, the existing `CONCURRENCY_LIMIT` blocks the excess plans. No retrospective evidence cell, future PnL, later OI, later crowding or later funding is allowed to reorder those historical decisions.

## Post-replay attribution

After the shared-capital replay is finished, the accepted portfolio trades are reconstructed through the same evidence pipeline used elsewhere:

```text
actual portfolio trade
-> decision-time EMA / ATR / momentum / breakout / turnover
-> OI delta at or before decision
-> account crowding at or before decision
-> prior settled funding at or before decision
-> stress classification
-> execution economics
-> realized PnL / PF / win rate / MFE / MAE / drawdown cells
```

This matrix is descriptive attribution of trades that were already selected. It exposes `evidence_used_for_historical_selection=false` and `historical_selection_uses_future_evidence=false`.

The independent per-symbol PR #64 evidence matrix remains the evidence source for the 10-minute live registry. The portfolio-trade matrix is intentionally not persisted over that live-ranking evidence snapshot because doing so would replace the broader evidence base with a slot-selected subset.

## Portfolio diagnostics

The artifact includes:

- opening/final equity, total return, PnL, PF, fees and drawdown from the canonical runner;
- eligible signal count versus accepted plan and entry counts;
- `CONCURRENCY_LIMIT` block count and its fraction of eligible signals;
- maximum concurrent positions;
- per-symbol trade count, win/loss count, win rate, PnL, PF and fees;
- maximum simultaneously committed initial entry notional reconstructed from actual trade intervals;
- the post-replay evidence matrix for actual accepted portfolio trades;
- the exact dynamic Top-10 market order and source-common start by symbol.

The initial-notional statistic is explicitly an entry-notional commitment diagnostic. It is not represented as mark-to-market gross exposure at every bar.

## Operational command

The approved runtime can run:

```bash
python -m tools.replay_bybit_source_common_period_portfolio \
  --site eu \
  --opening-equity 1000 \
  --output artifacts/bybit_source_common_portfolio.json
```

It requires the v113/v114 PostgreSQL history through `BYBIT_FULL_PERIOD_DATABASE_DSN`. The command refuses incomplete Top-10, incomplete v113 price coverage, incomplete v114 source-available derivatives coverage, missing synchronized bars, and any fixed-strategy config drift.

## Safety boundary

This is research and operator-review evidence only:

- `trade_actionable=false`;
- `operator_review_required=true`;
- `strategy_parameters_changed=false`;
- `parameter_retuning_performed=false`;
- `strategy_selection_allowed=false`;
- `strategy_promotion_allowed=false`;
- `demo_activation_allowed=false`;
- `live_activation_allowed=false`;
- `bybit_live_order_routing_allowed=false`.

It adds no create, amend or cancel order surface and makes no causal or profitability guarantee.
