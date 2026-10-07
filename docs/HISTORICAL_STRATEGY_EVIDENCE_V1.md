# Historical Strategy Evidence v1

Historical Strategy Evidence v1 answers a different question from ASTRA qualification:

**What would the shipped trading logic have done to capital on a fixed historical market-data snapshot after explicit execution costs?**

It does not grant trading authority.

## Separation of concerns

ASTRA keeps four questions separate:

1. Engineering correctness — does the system behave as specified?
2. Qualification — does a concrete build satisfy its required controls/profile?
3. Trading authority — is that build allowed to perform a live action?
4. Strategy evidence — did the strategy demonstrate economic value on historical/out-of-sample data?

A positive backtest can never set LIVE_ALLOWED, mutate TrustState or bypass qualification.

## Required historical inputs

Every promoted historical measurement must use:

- local immutable OHLCV snapshots;
- snapshot SHA-256 recorded in the manifest;
- exact symbol universe;
- exact time range;
- exact strategy/config SHA;
- no wall-clock-dependent decisions during replay;
- no hidden randomness unless the seed is explicit and recorded.

For Bybit perpetual studies, funding snapshots must be hash-locked separately.

## Portfolio economics

Historical portfolio replay must expose:

- opening cash;
- ending equity;
- total PnL;
- total return;
- maximum drawdown;
- turnover;
- maximum gross exposure;
- maximum concurrent positions;
- closed-trade count;
- win rate;
- profit factor.

Every closed trade must separately expose:

- entry reference price;
- entry execution price;
- exit reference price;
- exit execution price;
- entry fee;
- exit fee;
- slippage cost;
- gross PnL before execution costs;
- net PnL.

The invariant is:

gross PnL
- entry slippage
- exit slippage
- entry fee
- exit fee
= net trade PnL

## Cost coverage

Evidence records whether each cost layer is modelled:

- fixed fees;
- proportional fees;
- slippage;
- perpetual funding;
- queue position;
- partial fills.

For perpetual strategies, institutional cost completeness requires at minimum:

- proportional fees;
- slippage;
- funding.

Queue/partial-fill modelling remains necessary before passive maker execution can be treated as production-realistic.

## Profitability verdicts

Historical Strategy Evidence v1 may emit only:

- PROFITABILITY_NOT_PROVEN
- RESEARCH_EDGE_CANDIDATE
- OOS_EDGE_CONFIRMED

PROFITABILITY_NOT_PROVEN is mandatory when material cost coverage is incomplete.

RESEARCH_EDGE_CANDIDATE requires positive complete-cost evidence but does not require successful walk-forward/out-of-sample proof.

OOS_EDGE_CONFIRMED requires:

- complete material cost coverage;
- positive lower-bound cost-adjusted return;
- out-of-sample evidence;
- walk-forward evidence;
- return exceeding every declared benchmark.

None of these verdicts authorizes live trading.

## Benchmarks

At minimum, crypto portfolio evidence should compare against:

- cash;
- BTC buy-and-hold when BTC is in the universe;
- equal-weight universe;
- capital-matched equal-weight universe using the same gross-capital fraction as the strategy.

A strategy that earns a small positive return while dramatically underperforming a comparable passive benchmark has not demonstrated economic edge.

## Current research finding — 2026-10-07 control

A public Bybit daily-bar control was run over six liquid linear perpetuals:

- BTCUSDT
- ETHUSDT
- SOLUSDT
- XRPUSDT
- BNBUSDT
- DOGEUSDT

The current partial 2026-10-07 candle was excluded.

Window used by the portfolio replay:

- first execution: 2026-03-30
- last complete bar: 2026-10-06
- opening cash: USD 10,000

Legacy shadow economics:

- fixed fee: USD 0.50 per fill;
- slippage: 5 bps per side;
- ending equity: USD 10,185.64;
- total return: +1.856%.

Conservative taker-cost control:

- proportional fee: 8 bps per fill;
- slippage: 5 bps per side;
- ending equity: USD 9,977.45;
- total return: -0.226%.

Benchmarks over the same synchronized control window:

- six-symbol equal-weight full-capital: +26.458%;
- six-symbol equal-weight at 60% capital match: +15.875%;
- BTC buy-and-hold: +29.579%.

This control remains PROFITABILITY_NOT_PROVEN because:

- the live-retrieved bar set is not yet committed as a hash-locked snapshot;
- historical funding is not included in this short control;
- daily OHLCV cannot prove queue position or partial fills;
- it is one recent window, not walk-forward/OOS portfolio evidence.

The result is still useful: it shows that execution costs are material enough to turn the legacy positive shadow result slightly negative.

## Required next runs

1. Hash-locked three-year six-major portfolio replay.
2. Hash-locked three-year 21-symbol portfolio replay.
3. Funding-aware lower/upper bound.
4. Walk-forward/OOS portfolio replay.
5. Fee/slippage sensitivity matrix.
6. Maker/taker execution sensitivity with explicit fill probability.
7. Regime decomposition.
8. Capital-size sensitivity and turnover/capacity checks.

Only after these runs can strategy economics be discussed as anything stronger than research evidence.
