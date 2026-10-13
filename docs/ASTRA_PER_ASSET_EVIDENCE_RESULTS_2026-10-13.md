# ASTRA Strategy B research ledger: per-asset conditions and exits (2026-10-13)

**Evidence type:** reused/inspected historical daily Bybit sources only.
**Deployment authority:** None; `PROFITABILITY_NOT_PROVEN`.
**Provenance:** exactly these two successful GitHub Actions runs:
- [PR #315 full historical signals CI](https://github.com/PetrFedin/trade_bot/actions/runs/38447000601), SHA `1ae3850faa50d71d52ba3bba84f01ca3e0a73498`, artifact `per-asset-conditions-38447000601` (id 11839542475)
- [PR #316 full historical exit CI](https://github.com/PetrFedin/trade_bot/actions/runs/38447224888), SHA `79cb172bf5d34430e2e3d1a9c3e27123cc273691`, artifact `per-asset-exit-research-38447224888` (id 11838832380)

## Frozen historical control
Six symbols BNB, BTC, DOGE, ETH, SOL, XRP, a synchronized, long-only
top-two portfolio, USD 10,000 starting equity, 60% gross allocation cap,
29% equity target for each new position, 2% hard stop, 4% target,
2% trailing activation, 1.5% trailing distance and two-bar reentry.
Model assumptions: taker fee 8 bps **per fill** and fixed slippage
5 bps **per side**. 256 closed trades; historical total return
before funding **+0.7515525902%** (USD +75.155259);
funding timing-bounded lower **−1.8611011716%** and upper **+0.3192659666%**;
baseline max drawdown before funding **12.40084445%**.
No historical real order-book execution, queue, partial fills or exact
funding-account cashflows. Conservative daily OHLC same-bar threshold
collisions remain unresolved.

## 12 single-asset signal experiments, **before funding**
One and only one coin was assigned either stricter or looser prior-close
momentum/trend/volatility eligibility than the original Strategy A.
All five other symbols and capital/exit machinery remained unchanged.
Every cell below is the whole counterfactual portfolio return after
fees and fixed slippage; it is NOT that coin's isolated return.

| Symbol | STRICT: portfolio before funding | RELAXED: portfolio before funding | Closed trades STRICT / RELAXED |
|---|---:|---:|---:|
| BNBUSDT | +2.326% | -2.603% | 229 / 266 |
| BTCUSDT | +0.850% | -3.156% | 236 / 268 |
| DOGEUSDT | -2.625% | -4.790% | 260 / 263 |
| ETHUSDT | +2.333% | -5.567% | 242 / 271 |
| SOLUSDT | +5.925% | -16.692% | 256 / 272 |
| XRPUSDT | +1.469% | -1.139% | 242 / 267 |

**Findings (descriptive only):** All six RELAXED arms are worse than
the original control; five STRICT arms produce positive portfolio
pre-funding returns, but one (DOGE) is negative. This is *not*
a rule to universally tighten every coin, because no joint portfolio
optimization, historical funding or untouched validation is performed.
In particular SOL strict +5.925% is the strongest observed **single
change** in these 12 already-inspected-history arms, not a trade
recommendation, strategy prediction or net result.

## 24 single-asset exit experiments, conservative funding lower bound
Four one-coordinate changes per symbol, remaining five unmodified:
STOP_1P 1% stop, STOP_3P 3% stop, TARGET_3P 3% target, TARGET_6P
6% target. Existing trailing logic otherwise unchanged.
Each arm recomputes the **entire** path-dependent cash ledger, including
new entries and exits. Historic funding exposure is estimated within
uncertain settlement bounds on constant entry notional. The table uses
the lower (more conservative) return bound. **Positive numbers are
still not evidence of deployable expected profit.**

| Symbol | Single changed exit | Portfolio funding-adjusted **lower bound** | Delta vs Strategy A lower bound (percentage points) |
|---|---|---:|---:|
| BNBUSDT | STOP_1P | +0.314% | +2.175 p.p. |
| BNBUSDT | STOP_3P | -1.030% | +0.831 p.p. |
| BNBUSDT | TARGET_3P | -2.310% | -0.449 p.p. |
| BNBUSDT | TARGET_6P | +1.282% | +3.144 p.p. |
| BTCUSDT | STOP_1P | -5.580% | -3.719 p.p. |
| BTCUSDT | STOP_3P | -2.173% | -0.312 p.p. |
| BTCUSDT | TARGET_3P | -4.156% | -2.295 p.p. |
| BTCUSDT | TARGET_6P | +1.515% | +3.376 p.p. |
| DOGEUSDT | STOP_1P | -3.797% | -1.936 p.p. |
| DOGEUSDT | STOP_3P | -1.909% | -0.047 p.p. |
| DOGEUSDT | TARGET_3P | -4.303% | -2.442 p.p. |
| DOGEUSDT | TARGET_6P | -1.454% | +0.407 p.p. |
| ETHUSDT | STOP_1P | -2.520% | -0.659 p.p. |
| ETHUSDT | STOP_3P | +1.915% | +3.776 p.p. |
| ETHUSDT | TARGET_3P | -4.564% | -2.703 p.p. |
| ETHUSDT | TARGET_6P | +0.254% | +2.115 p.p. |
| SOLUSDT | STOP_1P | +1.193% | +3.054 p.p. |
| SOLUSDT | STOP_3P | -3.104% | -1.243 p.p. |
| SOLUSDT | TARGET_3P | -2.691% | -0.830 p.p. |
| SOLUSDT | TARGET_6P | -1.042% | +0.819 p.p. |
| XRPUSDT | STOP_1P | -1.173% | +0.688 p.p. |
| XRPUSDT | STOP_3P | -3.539% | -1.678 p.p. |
| XRPUSDT | TARGET_3P | -5.232% | -3.371 p.p. |
| XRPUSDT | TARGET_6P | +3.350% | +5.211 p.p. |

**Findings:** XRP TARGET_6P +3.350% portfolio lower-bound return and BTC
STOP_1P −5.580% are the extremes within the fixed 24-arm set.
The original 6-major lower bound is −1.861%, not +0.752% (which
excludes funding). Do not compare funding-adjusted exit-arm numbers
against the pre-funding signal-arm numbers as if they share a metric.
Do not combine the apparently favorable coin changes and add gains.

## Independent evidence gates and negative learning
1. These 36 arms all use **previously inspected** daily data. The
   multiple-comparison burden is nontrivial; apparent winners likely
   overstate repeatable advantage. The "best" variant MUST NOT be
   selected or promoted based on this table alone.
2. A meaningful final test requires historical rolling/nested training
   with *bounded, preregistered* parameter search, followed by
   separately frozen and **new untouched future** test observations.
3. Parameter changes affect subsequent available cash and future
   selection. All portfolio comparisons must preserve common timestamps,
   risk capital, costs, benchmarks and **full equity paths**.
4. Positive outcomes are sensitive to unresolved intraday threshold
   ordering, real spread, funding settlement timing, market impact,
   venue quantity constraints and tail-gapping risk.
5. Include losers, zero-trade variants, drawdown, turnover, capacity,
   trade-by-trade attribution, matched BTC/passive benchmarks and regime
   dispersion before any "profitability" conclusion.
6. Next [PR #317](https://github.com/PetrFedin/trade_bot/pull/317):
   ATR-normalized *entry-frozen* symbol-specific stops and targets
   calculated from 14 **completed prior** candles, capped exit levels,
   and validated exact portfolio control; no current execution bar
   lookahead, no change in production.
7. PR #313 and #314 separately investigate hourly historical
   stop/target ordering; they are not alternate actual fills.

## 12 additional ATR-normalized single-symbol exits (PR #317)

**New independent successful historical evidence**, not a fresh untouched holdout:
[exact-head Actions run 38450227591](https://github.com/PetrFedin/trade_bot/actions/runs/38450227591),
commit `5dfa00e58edc13ce3927fe52801c08704e6ea847`,
artifact `atr-frozen-research-38450227591` (id 11839977937).
18 core/ATR tests passed; full 12-arm replay and non-promotion checks
were GREEN. Model uses the **same six-major Strategy A control** and
conservative funding-adjusted *lower bound* as exit PR #316:
-1.861% for the original full portfolio.

Every experiment replaces only one coin's exit conditions. For each new
entry, the stop and target are derived from the prior 14 completed
true ranges divided by the prior completed close, then kept immutable
until exit. ATR_COMPACT = 0.50×ATR stop/1.00×ATR target;
ATR_EXTENDED = 0.75×ATR stop/1.50×ATR target, with stop
1–4%, target 2–8%, original trailing and max-holding rules.
When fewer than 15 completed price bars exist, the original fixed
policy is used and the fallback is logged. Full cash path, 8bps/fill
fees, 5bps/side slippage, funding timing bounds, source hashes,
ATR decision-price logs and negative cases are preserved.

| Single modified symbol | ATR_COMPACT portfolio lower | ATR_EXTENDED portfolio lower |\n|---|---:|---:|\n| BNBUSDT | +1.057% | +3.119% |\n| BTCUSDT | -6.747% | -1.617% |\n| DOGEUSDT | -2.818% | -2.122% |\n| ETHUSDT | -1.110% | +2.627% |\n| SOLUSDT | -1.322% | -1.547% |\n| XRPUSDT | +2.834% | +6.209% |\n
### Research interpretation — no parameter promotion
On this **already inspected** six-symbol history XRP ATR_EXTENDED
yielded +6.209% conservative portfolio lower-bound versus
−1.861% control. BTC ATR_COMPACT yielded −6.747%.
These are results of separate counterfactual **portfolio replays**
under fixed assumptions, not isolated coin returns, independently
executable order fills, expected future returns or additive strategy
improvements. Twelve additional comparisons increase data-snooping
risk; no winner may be selected or auto-deployed. The final
chronological validation and new untouched period still have not
occurred. DO NOT combine strong-looking profiles based on this table.

## Release status
All 48 historical arms: RESEARCH_ONLY, NOT AN UNTOUCHED HOLDOUT, NO AUTO WINNER,
NO BROKER/TRADING/OMS AUTHORIZATION. The project remains
`PROFITABILITY_NOT_PROVEN`.
