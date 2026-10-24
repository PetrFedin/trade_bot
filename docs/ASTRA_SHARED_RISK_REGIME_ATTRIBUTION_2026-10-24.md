# ASTRA / Shared portfolio risk and BTC regime attribution — 2026-10-24

## Research-only objective

Diagnose why the common USD 10,000 portfolio loses money under the already
fixed four candidate policies and their two signal rankers. Do not replace
Strategy A, alter active OMS/risk permissions, optimize a historical winner,
or publish false investor performance claims.

## Exact original dependency

The ledger must be reconstructed at exact commit SHA with the original
`tools.shared_capital_candidate_study` engine. Its original report SHA
must remain exactly:
`e2f8c37d5c6df948dccfbb054ad2941a443ba2b5b3015bdfadfee44ee82f9613`.

Original source: 21 Bybit DAILY OHLCV files, synchronized UTC 2023-10-09 through
2026-06-15, 981 days; 51 warmup days and 930 actual mark-to-market days.

For BTC regime attribution, separately load original raw BTCUSDT_D.csv
and require its entire byte-level SHA-256 to equal the SHA preserved in
the immutable 36-model shared report. The raw BTC archive may extend
beyond the matched 21-asset period; only its exact matched 981 UTC-day
intersection is admitted.

## Regime definition, no future data

For portfolio day T, at the UTC opening instant, examine only completed
BTC daily candles through T-1. Use BTC previous close vs its SMA50 and
20-trading-day close change:

- BTC_UPTREND: previous close exceeds SMA50 and previous 20-day
  close change is >= +5%.
- BTC_DOWNTREND: previous close is under SMA50 and previous 20-day
  close change is <= -5%.
- BTC_MIXED: all other cases.

Regime labels describe history and are not strategy filters or guarantees.
No parameter is tuned to the historical portfolio performance.

## Independently recomputed risk diagnostics per exact 36-model portfolio

- Daily NAV and cash/positions reconciliation; 10000 + cumulative daily
  modelled PnL equals terminal marked equity.
- Recalculate original peak-to-trough daily-close maximum drawdown.
- Worst single daily return and average of worst ceil(5% x 930) = 47
  observed daily returns. This is an empirical tail diagnostic; not
  a forecast, confidence bound or future risk probability.
- Longest consecutive days of closing equity below its previous peak.
- Closed-trade gross - entry+exit fee - modelled slippage = net.
- Gross fees, slippage, profit factor (undefined when no losses),
  stop/take counts, bottom 10% closed-trade net and per-coin PnL.
- Reconstruct rejected-entry reasons from actual daily decision traces.
- BTC regime PnL dollar changes and regime day counts, ensuring the
  three regimes sum to all 930 daily marks and exactly to terminal profit.
  Regime percentage returns are *averages of those days*, not
  additive or independent standalone strategy portfolio returns.
- Matched 8/5 vs 10/10 bps costs for 18 identical universe / arm / rank
  configurations. No assumption that adding cost must alter realized
  result by a fixed value, as order sizes depend on prior equity.

All inputs require immutable audit SHA and original venue-source timestamps;
all output receipt SHA-256 covers the full new JSON. The CI independently
recomputes the portfolio original from raw data and audits each model's
cash bridge, regime bridge and per-coin trade aggregation.

## Limitations

These are post-hoc descriptive observations on already inspected price data.
They do not establish causal explanations for returns or an untouched test.
No genuine funding-settlement Mark Price, verified account fee tier, real
broker execution, historical market-depth queue, partial fills or intraday
liquidation path are included. The account is simulated, unleveraged,
long-only. Daily-close drawdown understates potential intraday exposure.

New reports are research-only and cannot promote candidates or authorize
demo/live trading. `PROFITABILITY_NOT_PROVEN`.

## Follow-up work, never automatic production promotion

Explore frozen ex-ante BTC market-state position-sizing/gating as independent
new candidates, require prefix-invariance tests, compare shared cash and
complete execution expenses, retain losing combinations, and validate on
an actually uninspected subsequent market window when available. Always
qualify Mark Price and funding separately before calling perpetual-futures
results exchange-realistic.
