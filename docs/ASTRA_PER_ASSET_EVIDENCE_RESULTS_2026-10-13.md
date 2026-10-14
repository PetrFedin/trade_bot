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

## Chronological nested selection diagnostic — 2026-10-14, PR #319

**A deliberately adverse result, preserved without retuning.**
[Exact-head GREEN GitHub Actions run 38599307148](https://github.com/PetrFedin/trade_bot/actions/runs/38599307148)
on `c9e2960cd3bfee0386f96b904082e8148956720d`; evidence
`019c7e468aee4687807c85643596548bbff4f8fdcfaa899a64c02ca34a7bee0d`.
Artifact `atr-nested-development-38599307148` id **11911358668**.
13 lint/unit/integration tests passed, all seven outer folds completed,
all 84 inner candidate rows and their rejection reasons archived,
and negative outcomes included. This is **development pseudo-OOS**:
these same 2023–2026 market dates and ATR families were previously
inspected by ASTRA; it is *not* an independent untouched holdout.

**Fixed prior-only selection scheme**: 420 completed daily bars as
outer history, last 90 of those bars for inner selection, then a distinct
non-overlapping 90-day outer validation. Each fold selects independently
for the six symbols only from BASELINE, ATR_COMPACT and ATR_EXTENDED
using the 12 arm inner full-portfolio results, strict minimum 3 symbol
trades, >0.5 percentage point inner funding-lower advantage versus
Strategy A, and <=2 percentage points extra drawdown. Chosen profiles
are frozen before outer test; true-range-based stops/targets are frozen
per entry. Same starting $10k, fees 8bps/fill, slippage 5bps/side,
funding timing bounds, unchanged gross exposure and original selectors.

| Outer test | Strategy A conservative lower | Adaptive conservative lower | Adaptive difference (percentage points) |
|---|---:|---:|---:|
| 1 | -3.3815% | -3.2809% | +0.1007 |
| 2 | -3.9734% | -3.9734% | 0.0000 |
| 3 | +3.5754% | +3.1737% | -0.4017 |
| 4 | +0.7131% | +0.2473% | -0.4659 |
| 5 | -2.9775% | -2.9775% | 0.0000 |
| 6 | +0.5740% | +0.5740% | 0.0000 |
| 7 | -3.2503% | -4.2277% | -0.9774 |

Report's mean outer fold **adaptive lower advantage -0.249193
percentage points**; just **1/7** outer folds beat Strategy A,
**3/7** tied because all six symbols fell back to BASELINE, and **3/7**
underperformed. Three adaptive outer folds had a positive funding-lower
return, yet this is insufficient relative to control and benchmarks.
Worst adaptive outer funding-lower return was **-4.2277%**. Its
reported average lower return per *restarted fold* was **-1.4949%**.
DO NOT compound/sum these seven window returns into a continuous
capital track: each 90-day evaluation restarts with $10k.

**Consequence:** The ex-ante adaptive selection algorithm tested here
**FAILED TO IMPROVE** the unchanged Strategy A reliably. Do not
promote XRP ATR_EXTENDED or any apparent old full-period winner. Do
not alter the 0.5/2.0 percentage-point gates retrospectively to
make these same outer-fold outcomes look favorable. Document next
candidate families as *new hypotheses*, hold their search budget
fixed, test on genuinely new data after freeze, and retain the
conservative intrabar-execution/venue funding uncertainties.
Also retain the 10bps fee/10bps slippage *pre-funding-only* stress
fields separately from the funding-adjusted primary table.

## Candidate B completed-bar impulse veto discovery — 2026-10-15

**Full-portfolio evidence**: [GREEN exact-head run 38646276271](https://github.com/PetrFedin/trade_bot/actions/runs/38646276271),
head `5a1211e55919554b333ab05cd2dc934f5ff2611f`,
artifact `candidate-b-impulse-38646276271` ID **11932607670**,
evidence SHA-256 `43be7151e050fc623f184b9ac501d070cae34e678766edd84ec6803c277f4851`.
13 fast regression tests and all 14 genuine historical six-major
portfolio replays passed. Everything uses **previously inspected**
2023-2026 daily OHLCV, estimated 8bps/fill fees, 5bps/side slippage
and conservative timing-bounded funding, not real exchange fills.
Control: 256 closed trades, 158 entry-bar exits, **−1.861%**
conservative portfolio lower-bound after funding.

Before seeing economic results, the study fixed:
- PRIOR_1D_ATR_150: veto new coin entry when positive return on last
  completed day > 1.50 × 14-day gap-aware ATR / last completed close.
- PRIOR_2D_ATR_225: veto new coin entry when positive preceding
  two-day return > 2.25 × the same relative ATR.
- 12 single-coin changes, plus 2 independently preregistered joint
  six-coin changes; no retrospectively assembled portfolio.
- Existing holdings, stop 2%, target 4%, trailing, entry ranking,
  portfolio risk 60% and capital cash accounting unchanged.

| Scope | Hypothesis | Lower-bound portfolio return after funding | Delta vs A (p.p.) | Blocked new entries | Entry-bar exits | Closed trades |
|---|---|---:|---:|---:|---:|---:|
| BNBUSDT | PRIOR_1D_ATR_150 | -3.889% | -2.027 | 4 | 157 | 254 |
| BNBUSDT | PRIOR_2D_ATR_225 | -2.038% | -0.177 | 6 | 157 | 254 |
| BTCUSDT | PRIOR_1D_ATR_150 | -2.711% | -0.850 | 8 | 157 | 253 |
| BTCUSDT | PRIOR_2D_ATR_225 | -3.993% | -2.132 | 12 | 157 | 252 |
| DOGEUSDT | PRIOR_1D_ATR_150 | -3.594% | -1.733 | 1 | 158 | 256 |
| DOGEUSDT | PRIOR_2D_ATR_225 | -6.566% | -4.705 | 3 | 157 | 256 |
| ETHUSDT | PRIOR_1D_ATR_150 | -1.175% | +0.687 | 3 | 160 | 256 |
| ETHUSDT | PRIOR_2D_ATR_225 | -2.961% | -1.099 | 4 | 158 | 255 |
| SOLUSDT | PRIOR_1D_ATR_150 | -1.647% | +0.214 | 1 | 157 | 256 |
| SOLUSDT | PRIOR_2D_ATR_225 | -2.134% | -0.273 | 1 | 158 | 256 |
| XRPUSDT | PRIOR_1D_ATR_150 | -2.964% | -1.103 | 2 | 157 | 255 |
| XRPUSDT | PRIOR_2D_ATR_225 | -2.349% | -0.488 | 6 | 156 | 254 |
| ALL 6 | PRIOR_1D_ATR_150 | -6.588% | -4.727 | 19 | 156 | 250 |
| ALL 6 | PRIOR_2D_ATR_225 | -10.466% | -8.604 | 32 | 153 | 247 |

**Observed negative discovery**: Just **2/14** arms (single ETH
one-day and single SOL one-day) had a funding-lower return higher
than original A, but neither made a positive conservatively
funding-adjusted portfolio return. The other **12/14** worsened
relative to A. Joint one-day filter **−6.588%**, joint two-day
filter **−10.466%**. Importantly the two-day joint filter blocked
32 proposed entries and reduced entry-bar exits from 158 to 153,
yet portfolio profitability deteriorated sharply. A naive
`fewer same-day stops == improvement` metric is **REJECTED**.

**Policy:** Do not grant Candidate B a production, paper, testnet or
broker permission. Do not adjust the ATR thresholds to maximize
these previously inspected losses. [Draft PR #322](https://github.com/PetrFedin/trade_bot/pull/322)
was preregistered to test **past-only** profile selection across
nonoverlapping later historical windows and must disclose negative
results, baseline fallback, fee/slippage/funding and zero-trade
falsification. Even a positive nested diagnostic will remain
pseudo-OOS until genuinely new independently sourced dates
pass the [post-freeze gate PR #320](https://github.com/PetrFedin/trade_bot/pull/320).

## Candidate B past-only nested walk-forward — 2026-10-15, PR #322

**Independently reproducible NEGATIVE / NO-OP evaluation:**
[exact-head GREEN run 38646974925](https://github.com/PetrFedin/trade_bot/actions/runs/38646974925),
commit `c5d48df9103d64d1f205247e8e25562981e9cf84`,
archived artifact `candidate-b-nested-38646974925` (id **11932337978**),
evidence SHA-256 `62fdab742d156347b5d4f1e31a7a498ba936c0fe903a8a643ccd79d7b932fc73`.
All 13 tests passed; seven later chronological 90-day fold portfolios
and all 84 past-only inner candidate replays (six coins × two entry
vetoes × seven historical folds) completed. All histories pre-inspected
in other ASTRA experiments: pseudo-OOS for development, NOT a truly
untouched final forward holdout.

**Results:** Strategy B produced **0/7 positive incremental fold wins
against unchanged Strategy A, 7/7 exact portfolio ties, 0/7 regressions**.
The *mean incremental portfolio funding-adjusted conservative return*
was exactly **0 percentage points**. Four folds picked BASELINE on
every symbol. In three folds one symbol's prior-impulse rule was
conditionally admitted based on past-only validation (XRP 2D in fold
0, BNB 1D in fold 3, BTC 2D in fold 5); those selections did NOT
veto any subsequently entered position or modify cash/PnL in the
later 90-day test window. This does **not** confirm predictive
quality of the rule: its realized marginal effect was zero.

| Later outer fold | Original A lower | Candidate B lower | Difference |
|---|---:|---:|---:|
| 1 | -3.3815% | -3.3815% | 0.0000 p.p. |
| 2 | -3.9734% | -3.9734% | 0.0000 p.p. |
| 3 | +3.5754% | +3.5754% | 0.0000 p.p. |
| 4 | +0.7131% | +0.7131% | 0.0000 p.p. |
| 5 | -2.9775% | -2.9775% | 0.0000 p.p. |
| 6 | +0.5740% | +0.5740% | 0.0000 p.p. |
| 7 | -3.2503% | -3.2503% | 0.0000 p.p. |

Every 90-day fold restarts capital at USD 10,000; never
sum/compound these seven reports into one live-account return.
All quoted values use 8bps/fill fee, 5bps/side slippage and
historical constant-notional funding timing **upper cost bound**.
Additional 10/10 fee/slippage stress was collected **before**
funding only. Neither the future period nor venue account fills
have been independently established.

**Decision:** No evidence that this Candidate B profile selection
improves historical portfolio economics. Do not loosen entry gates
retrospectively or enable paper/broker/live; retain this complete
negative/falsifying example in the permanent research ledger.
[Post-freeze source gate #320](https://github.com/PetrFedin/trade_bot/pull/320)
is GREEN in CI, but contains NO independently verified later
90-day research dataset.

## Release status
All 62 previous historical arms plus 84 ATR and 84 entry nested inner evaluations: RESEARCH_ONLY, NOT AN UNTOUCHED HOLDOUT, NO AUTO WINNER,
NO BROKER/TRADING/OMS AUTHORIZATION. The project remains
`PROFITABILITY_NOT_PROVEN`.
