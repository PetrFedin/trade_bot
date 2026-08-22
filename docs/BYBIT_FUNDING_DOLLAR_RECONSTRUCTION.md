# Bybit funding dollar reconstruction for strategy replay

## Purpose

This layer converts historical funding-rate events into a USDT impact for the existing research replay without changing the production strategy or claiming that the result is the broker's authoritative account ledger.

The reconstruction uses three already separated pieces of evidence:

1. replay trade quantity persisted by the strategy simulator;
2. Bybit public funding-history settlement timestamps and rates;
3. Bybit public historical mark-price candles.

## Bybit funding economics

For a USDT perpetual the reconstruction uses:

```text
position_value_usdt = quantity * mark_price_usdt
funding_amount = position_value_usdt * funding_rate
```

Funding PnL is represented from the strategy position's perspective:

- positive funding rate: LONG pays, SHORT receives;
- negative funding rate: LONG receives, SHORT pays.

Therefore:

```text
LONG  funding_pnl = -(position_value * funding_rate)
SHORT funding_pnl = +(position_value * funding_rate)
```

## Settlement-price contract

`app/marketdata/bybit_mark_price_history.py` reads only public `GET /v5/market/mark-price-kline` data.

A funding settlement is reconstructed only when a mark-price candle exists with:

```text
candle.start_time == funding_timestamp
```

The candle **open** at that exact timestamp is used as the settlement mark-price proxy. The implementation does not interpolate, use a later candle, backfill from a future observation, or substitute entry price.

If an exact mark-price point is unavailable, that trade's dollar funding attribution remains incomplete.

## Trade interval contract

Funding is attributed only for events satisfying:

```text
entry_time < funding_timestamp <= exit_time
```

A trade that does not cross a funding settlement has a complete reconstructed funding impact of zero and does not require mark-price history for a nonexistent settlement.

## Research output

The dynamic Top-10 research artifact now reports:

- number of trades with complete funding reconstruction;
- incomplete trade count and explicit missing reasons;
- number of trades crossing funding settlements;
- total settlement count;
- replay net PnL for complete trades;
- reconstructed funding PnL;
- replay PnL after reconstructed funding;
- funding impact by symbol and by LONG/SHORT side.

## Reconstruction is not broker-ledger reconciliation

The calculation is a deterministic reconstruction from public Bybit history. It must not be interpreted as an authoritative statement that the exchange credited or debited the same amount in the real account.

Authoritative broker-ledger reconciliation is a separate future step and must compare the reconstruction with read-only real-account transaction/funding records from the isolated mainnet account boundary.

Until that comparison exists:

- public-history funding reconstruction can be complete;
- broker funding ledger reconciliation is **not** claimed;
- production accounting remains unchanged;
- no research result can authorize a live order.

## Safety boundary

This stack adds no authenticated write capability and no order method. All research outputs retain:

- `parameter_retuning_performed=false`
- `strategy_selection_allowed=false`
- `strategy_promotion_allowed=false`
- `demo_activation_allowed=false`
- `live_activation_allowed=false`
- `bybit_live_order_routing_allowed=false`
- `causal_claim_allowed=false`
- `predictive_guarantee_allowed=false`

The canonical trading runtime remains Bybit Demo Trading only. The real-account boundary remains read-only.
