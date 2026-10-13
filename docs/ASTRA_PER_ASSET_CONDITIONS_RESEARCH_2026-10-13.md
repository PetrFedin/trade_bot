# ASTRA per-asset adaptive conditions — registered research programme

Date: 2026-10-13. Status: **RESEARCH ONLY / PROFITABILITY_NOT_PROVEN**.

## Rationale
The historical Strategy A uses a uniform 2% hard stop, 4% target,
2% trailing activation, 1.5% trailing distance and the same momentum and
volatility signal thresholds for every coin. Such uniformity is an
architectural restriction, **not evidence that per-symbol tuning is better**.
Investigate heterogeneity without post-hoc survivor selection.

## First implementation / fixed diagnostic
`tools/per_asset_conditions_lab.py` runs 12 predeclared conditions on
the original six-major source set. For each of BTC, ETH, BNB, SOL, XRP,
DOGE independently:

- STRICT_MOMENTUM: min three-bar momentum +0.8%, min trend strength +0.4%,
  maximum realized volatility 2%.
- RELAXED_MOMENTUM: minimum momentum 0%, trend 0%, maximum volatility 5%.
- Other five symbols keep unchanged default signals and the exact same
  top-2 cross-sectional rank algorithm, position management, risk and cash
  accounting. Baseline A is reproduced independently.
- Same starting USD 10,000, 8bps/fill and 5bps/side slippage. All arms and
  negative outcomes are emitted. No winner is auto-selected or promoted.
- Prior completed daily bars are the only signal input; today’s high/low
  is never used to choose today's entry.

The fixed arm parameters here are illustrative **hypotheses**, not
symbol-specific empirical optima. Funding is **not included in this first
research diagnostic**, so these arms do not establish net profitability.
The historical series are previously inspected; do not relabel as OOS.

## Next implementation: genuinely adaptive per-instrument policies
Design next research variants using **only preceding history**:

1. ATR / historical realized-volatility multiples by symbol for stop,
   target, trailing and position size; cap hard stop and gross exposure.
2. Trend-versus-range regime selection using previous completed closes,
   ADX, moving average slope, volatility percentile and volume quality.
3. Separate fixed entry hypotheses for breakout, pullback, mean reversion
   and impulse exhaustion. Long and short tested separately if order/borrow
   semantics are verified.
4. Independent regime and capital-cost stress. Measure stale candles,
   gaps, collision-first-touch, fee tiers, funding windows, spread,
   volume participation, order-book impact and risk-of-ruin proxies.

Do **not** allow per-symbol parameters to reset position protection without
an approved exact-period rule. All training uses chronological data and
predeclared search budgets, with nested walk-forward: hyperparameters
selected inside past training only, then frozen for a distinct test window.
Compare against untouched shipped baseline on the *same synchronized portfolio
clock*, capital, allocations and market exposures. Count all tested arms
when reporting selection bias. Evaluate cash alternative and capital-matched
benchmarks and confidence uncertainty. Preserve losing arms.

## Promotion gates
1. Validate source SHA, source revisions, time alignment and trading costs.
2. Non-vacuous trades and non-overlapping folds across market regimes.
3. Full path-dependent portfolio PnL with funding timing bounds and drawdowns.
4. After final candidate freeze, genuinely new untouched future data,
   plus shadow/paper execution replay with venue-rule and risk controls.
5. Independent review. **Research findings never enable broker writes**.

No production/config changes, no additional paid resources and no live
activation from this research programme.
