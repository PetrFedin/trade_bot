# ASTRA Candidate B — nested prior-impulse selection development experiment

Date: 2026-10-15. **Draft, research-only, no strategy promotion.**

Dependency: [Draft PR #321](https://github.com/PetrFedin/trade_bot/pull/321)
provides a backtester-only, prior-completed-candle new-entry veto.
This nested experiment MUST NOT merge ahead of its dependency and
independent code/data review.

## Frozen search budget (registered before viewing #321 PnL)

For the six pinned Bybit symbols BNB/BTC/DOGE/ETH/SOL/XRP, compare
exactly two previously specified entry veto hypotheses per coin:
- PRIOR_1D_ATR_150 — prevent new entry after a prior one-day
  positive return greater than 1.50 × ATR14/last completed close;
- PRIOR_2D_ATR_225 — prevent new entry after a prior two-day
  positive return greater than 2.25 × ATR14/last completed close.

No other rule is searched, no per-symbol numeric grid is optimized,
and all historical negatives are retained. With fewer than
15 completed OHLC bars, original entry is allowed. Existing positions
are *never* closed by the veto; ranking, exits, stops, reentry and
leverage protection remain original Strategy A.

## Nested chronological procedure

Pinned six symbol daily archive 2023-10-09–2026-10-08 plus authentic
self-described Bybit funding snapshot; all these historic dates
were inspected in earlier ASTRA research. Outer evaluation is
**pseudo out-of-sample for development**, not truly untouched
unobserved future data.

For each of seven possible rolling outer folds:
1. Use 420 prior completed daily bars only.
2. Reserve final 90 of those historical bars for a fresh-account
   **inner** comparison with the same synced Strategy A control,
   warming up on preceding 330 bars.
3. For each of six symbols, replay each of its two entry vetoes
   (12 independent inner portfolios). All other coins use original
   entry/exit; full cash, capital competition, funding timing bounds,
   drawdown, fees and losses are recomputed.
4. Select one symbol-specific profile only if the inner validation
   shows at least one actually rejected entry, at least two
   closed trades for the modified symbol, at least eight
   portfolio trades total, conservatively funding-adjusted whole
   portfolio improvement **greater than 0.5 percentage points**
   and drawdown deterioration <=2 percentage points.
   Otherwise **BASELINE**. Ties choose larger inner improvement
   then alphabetic hypothesis name. These admission thresholds
   are predeclared practical guards, not estimated optima.
5. Freeze the resulting six-symbol profile map and run a combined
   portfolio on the NEXT 90 previously unseen *by that fold*
   historical daily execution bars. Original Strategy A replays
   over the exact same window with a fresh $10,000 account.
6. Move forward exactly 90 bars for the next outer fold;
   later test intervals must not overlap. Overlapping past context
   is allowed. All seven results, including losses and no-change,
   are preserved without retroactively adjusting thresholds.

A cached copy of prior-bar top-two selections is used to keep
the 7 × 12 inner experiments efficient. Each fold must first
reproduce the original selector **exactly**, including all closed
trades, entry decisions and ending equity. The candidate filter
sees the previous completed OHLC, never the execution day's
high/low/close.

## Accounting and limitations

Original six-major strategy policy: opening cash $10,000,
max gross exposure 60%, new position 29% prior-close equity,
no leverage, two eligible reentry bars, 2% hard stop,
4% take profit, 2% trailing activation, 1.5% trailing gap,
max 10 daily holding bars. Price execution model uses
8 basis points fee **per fill**, 5bps slippage per side.
Funding costs are bounded from archived settlement dates,
using uncertain timing and constant entry execution notional,
not venue-exact mark price.

Track return before funding, conservatively funding-adjusted
lower and upper, all trade/entry-veto counts, same-entry-bar
exits, intrabar stop/target ambiguity, capital utilization,
drawdown, turnover and cost sums per fold. Compare against
Strategy A, cash, BTC full buy-hold and 60%-capital-matched
passive equal-weight buy-hold (latter **before** passive
transaction costs and funding, not a fair net alpha benchmark).
Separately test frozen profiles and Strategy A at 10bps/fill
and 10bps/side slippage; stress is **pre-funding only** and
must not be mislabeled net.

This is a procedure-qualification test. An apparently positive
mean across fold results is NOT compounded account growth, as
each outer fold begins with a newly reset $10k portfolio.
It cannot establish deployable return: posthoc human hypothesis
choice, previously inspected dates, unverifiable intraday
touch order and modelled spreads/funding remain limitations.

### Acceptance, subsequent priority, release boundary

- Exact-head GitHub Actions, reproducible all-fold artifact,
  source SHA-256 (six prices + six funding files), deterministic
  profile selection, test-suite safety invariants.
- No missing or skipped fold, zero-trade variant admission,
  false out-of-sample claim or auto-promotion.
- Underperforming folds are as important as improvements.
- Regardless of preliminary result, future 90-day post-freeze
  entirely new data cannot be admitted until
  [PR #320 temporal source gate](https://github.com/PetrFedin/trade_bot/pull/320),
  independent provider-source provenance and frozen Candidate B
  configuration are verified.
- No cash order routing, paper trading authorization, broker,
  release/main merge or paid infrastructure introduced.

**`PROFITABILITY_NOT_PROVEN` / production HOLD.**
