# ASTRA — Nested per-asset ATR walk-forward, research qualification v1

Date: 2026-10-14. Research-only. Status before independent CI:
**candidate experiment prepared; profitability has not been proven.**

This implementation follows the full 48-arm per-symbol research census in
PR #315–#318. Some candidate arms improved modeled funding-adjusted
historical results on data already inspected by researchers, so choosing
the winner and calling it validated would be data snooping. We instead
test a bounded **chronological adaptive-selection procedure**.

## Two levels of time separation

The source is the genuine pinned Bybit 2023-10-09–2026-10-08 daily
six-major OHLCV archive plus historical funding settlements, identified
by all twelve SHA256 digests. This history has ALREADY been inspected
in many earlier ASTRA studies: even time-ordered outer folds are
*development pseudo-OOS*, NOT a truly untouched future holdout.

Outer fold policy: 420 daily bars of past history, then 90 daily
execution/test bars, next outer fold advances 90 bars. Overlapping
420-bar training contexts are permitted, but the seven 90-day
outer tests are strictly nonoverlapping. Capital restarts at
USD 10,000 for each external evaluation; never compound them into a
fake continuous 7-fold account.

Inner profile selection per outer fold: The last 90 **of those 420
past training bars** are an inner validation window. The first 330
past bars warm up signals and ATR. A fresh account is opened only at
the beginning of inner validation. Profile selection occurs solely
from these inner-validation results. All decisions are frozen BEFORE
the subsequent 90-bar outer test. Outer-test outcomes are **never**
fed back into that fold's profile choice.

All 12 registered arms are recomputed on the SAME synchronized
six-coin cash ledger. Each changes ONLY one symbol to
ATR_COMPACT or ATR_EXTENDED and leaves all other symbols at shipped
2%-stop/4%-target rules. The original cross-sectional selected names,
calculated from previous completed bars, are cached only after a
second full replay proves **exact parity** with the source backtester.

For each symbol, choose one of its two arms only if:
1. At least three of that symbol's trades closed in the inner window.
2. That symbol's trade exit timing actually differs from Strategy A.
3. Improvement in **conservatively funding-adjusted** FULL
   portfolio return exceeds 0.50 percentage points.
4. Maximum marked-equity drawdown is no more than 2.00 percentage
   points worse than Strategy A.

Otherwise keep that symbol at Strategy A / BASELINE.
Tie-breaks are deterministic, preferring larger inner conservative
return improvement then alphabetical profile name.
These gate levels are predeclared **hypotheses**, not empirically
optimized values. Every rejected arm, score and reason is retained.

A single combined per-symbol profile map selected independently
within the inner past is frozen for the subsequent external fold.
The ATR value itself is still recalculated at each **new entry**
from 14 genuine already completed candles; the resulting stop and
target are immutable throughout that holding. Existing risk budget,
reentry protection and exposure caps are unchanged.

## Costs, comparisons and failure cases

Every inner and outer arm replays real portfolio equity, entries,
exits and capital competition with USD 10k initial equity, a
60% gross cap, a 29% prior-close equity target, 8bps/fill fees and
5bps/side assumed fixed execution slippage. Conservative funding
timing bounds are recomputed on each portfolio's actual trades and
open positions. These are approximations of venue-settlement costs,
not account-authoritative payments.

Outer benchmark report includes Strategy A on the IDENTICAL
window/costs, cash, 60%-capital-matched equal-weight passive six
coin buy-and-hold, and unlevered full-equity BTC buy-and-hold.
Stress checks freeze the profiles again and independently replay
Strategy A vs adaptive combined portfolio at 10bps/fill fees
and 10bps/side slippage. Funding is **explicitly excluded from
stress**, not mislabeled as net stress profit.

The report has each fold's complete 12 inner arms, profile admission
reasons, actual selected profiles, original versus adaptive outer
return lower/upper funding bounds, trades, drawdowns, turnover,
fee sums, conservatively ambiguous OHLC stops, entry prior-time
audit, benchmarks and separate stress PnL. It emits source hashes,
one deterministic evidence hash and all negative results.

## Important methodological limitations

- The entire data range was previously inspected and humans defined
  the ATR arms after looking at preceding results, so this is
  **not fully independent OOS**. Its primary purpose is to reject
  fragile methods and locate known failure conditions.
- The BTC or SOL portfolio performance can change solely through
  competing opportunities; individual winners are not additive.
- Optimizing a total-return delta for each symbol separately on the
  same original portfolio does not ensure the combined strategy
  works; that is why the six selected profiles are then replayed
  together on strictly later outer days.
- With only daily OHLC, true within-day stop/target touch ordering,
  spreads, queue priority, partial fills, account funding and
  venue position limits are unknown. Fill quality remains assumed.
- The worst observed fold, no-trade folds, drawdowns and negative
  relative benchmark performance must be disclosed.
- Multiple comparisons and selection bias remain; never choose
  *this* procedure's thresholds by inspecting its outer results.

### Hard release boundary

Do not merge directly into main while dependent PR #316 and #317
are still Draft/unreviewed. Exact-head CI and a separate human or
agent reviewer must inspect code, data, economic artifacts and
negative cases. The outer folds DO NOT grant live, paper,
broker/OMS, leverage or real-money authority. After a fixed
candidate passes development gates, freeze its rules and SHA
before receiving a **genuinely later, newly collected** market
interval. Do not recycle 2023–2026 history as that final holdout.

`PROFITABILITY_NOT_PROVEN` and production HOLD remain.
