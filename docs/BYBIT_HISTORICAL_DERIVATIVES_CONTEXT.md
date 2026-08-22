# Bybit historical derivatives context

## Purpose

This layer adds derivatives-market evidence to each historical strategy decision without allowing future data to leak into the entry feature set.

It complements, rather than replaces, the price/turnover/EMA/ATR diagnostics from the dynamic Top-10 research stack.

## Public Bybit series

The client uses only public GET endpoints:

- `/v5/market/open-interest`
- `/v5/market/account-ratio`
- `/v5/market/funding/history`

The client has no authenticated secret and no order/write method.

### Open interest

For linear USDT contracts Bybit reports open interest in units of the base asset. For example BTCUSDT OI is BTC, not USDT. ASTRA therefore keeps the raw OI quantity and its point-to-point delta rather than pretending it is directly comparable across different coins in dollar terms.

### Long/short account ratio

Bybit defines `buyRatio` and `sellRatio` as the fractions of position holders who are net long or net short. This is account-holder crowding, not the share of notional position size.

ASTRA stores:

- long account ratio;
- short account ratio;
- long/short account ratio when the short ratio is non-zero;
- a descriptive crowding regime: `LONG_HEAVY`, `BALANCED`, or `SHORT_HEAVY`.

### Funding

Funding rates settle on symbol-specific schedules. The history client therefore does not assume one global interval.

For each trade ASTRA separates:

- the last settled funding rate already known at or before `decision_time`;
- funding-rate events that occur after entry and before/at exit.

The second set is post-entry attribution only. It is never allowed to become an entry feature.

## No-lookahead join

For every closed replay trade:

1. identify the original ENTRY event and exact `decision_time`;
2. select the latest OI point with `timestamp <= decision_time`;
3. select the immediately preceding OI point to measure OI change;
4. select the latest account-ratio point with `timestamp <= decision_time`;
5. select the latest settled funding point with `timestamp <= decision_time`;
6. separately collect funding events with `entry_time < funding_timestamp <= exit_time`.

If data is missing, the context records explicit missing reasons instead of filling or interpolating a value from the future.

## Historical associations reported

The diagnostics compare realized net PnL/MFE/MAE across:

- `OI_RISING`, `OI_FALLING`, `OI_FLAT`, `OI_UNKNOWN`;
- `LONG_HEAVY`, `BALANCED`, `SHORT_HEAVY`, `CROWDING_UNKNOWN`;
- positive, negative, zero, or unknown prior funding;
- LONG versus SHORT;
- repeated combinations of side + OI regime + crowding + prior funding.

Patterns are historical associations only. They do not establish causality or a guarantee that the same condition will be profitable in the future.

## Resolution

The one-command Top-10 research run joins derivatives context at 1-hour resolution around the 5-minute execution replay. This keeps API pagination tractable while preserving point-in-time derivatives state.

A one-day warm-up is acquired before the first 5-minute replay bar so the first decisions can have a previous OI point and a prior settled funding observation when Bybit history provides them.

## Remaining evidence gaps

The current stack deliberately leaves three items unresolved:

1. **Dollar funding cost.** Holding-period funding rates are collected, but they are not yet reconciled against exact replay position notional at each settlement timestamp. Until that is done, funding is context rather than fully reconciled PnL cost in the research replay.
2. **Liquidation history.** Liquidation/stress events are not yet joined to each signal timestamp.
3. **Historical order-book depth.** Ordinary V5 kline history cannot reconstruct past depth. Current top-of-book liquidity must not be represented as historical depth.

## Safety boundary

All outputs retain:

- `parameter_retuning_performed=false`
- `strategy_selection_allowed=false`
- `strategy_promotion_allowed=false`
- `demo_activation_allowed=false`
- `live_activation_allowed=false`
- `bybit_live_order_routing_allowed=false`
- `causal_claim_allowed=false`
- `predictive_guarantee_allowed=false`

The real-account connection remains the separate read-only observability boundary from PR #50. The canonical order-writing product runtime remains Bybit Demo Trading only.
