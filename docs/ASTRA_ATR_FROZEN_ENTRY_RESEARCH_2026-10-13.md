# ASTRA — ATR-normalized exits fixed at individual position entry

Date: 2026-10-13. Status: **DRAFT / RESEARCH ONLY / PROFITABILITY_NOT_PROVEN**.
Depends on PR #316 for optional per-symbol position exit policies.
Further extension in this isolated branch **must not** change production trading.

## Specific hypothesis
Different instruments can exhibit different daily price distributions, so
the same 2% stop / 4% target need not be the highest-quality hypothesis
for all. This **does not establish** that any ATR-derived rule makes money.

## Research-only code and temporal proof
`CrossSectionalPortfolioBacktester` now accepts an explicitly optional
`research_entry_policy_resolver(symbol, prior_completed_symbol_bars)`.
When absent, the original fixed exit rules run unchanged. When present,
the resolver is called **before an entry fill**, strictly with each symbol's
completed historical bars ending before the current execution day. The
validated `PositionManagementPolicy` is retained in immutable
`_OpenPositionState.entry_frozen_policy` through the entire holding, for
all take-profit, stop, trailing and time exits. It is **not recalculated**
using subsequent candles while the position is open.

The research tool computes 14 previous true ranges:
`TR[t] = max(high[t]-low[t], |high[t]-close[t-1]|, |low[t]-close[t-1]|)`.
ATR is their arithmetic mean; relative ATR = ATR / last completed
close. It requires 15 completed bars. With insufficient history, use
the exact Strategy A policy and log the fallback (rather than a
shortened ATR history). The execution day's current high/low/close
never enters ATR. All OHLC validations, time ordering and fund history
fail closed.

### Ex-ante fixed experiment grid
Universe: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, DOGEUSDT,
each with the original top-2 prior-bar ranking. For **each** coin
independently test two predeclared alternatives, keeping other five
coins on original exit rules:

| Profile | Stop | Target | Limits |
| --- | --- | --- | --- |
| ATR_COMPACT | 0.50 × relative ATR | 1.00 × relative ATR | stop 1–4%, target 2–8% |
| ATR_EXTENDED | 0.75 × relative ATR | 1.50 × relative ATR | stop 1–4%, target 2–8% |

These values are **not statistical estimates of optimal parameters**.
They are preregistered sensitivity hypotheses. Trailing parameters
remain +2% activation / 1.5% peak stop, maximum hold 10 daily
bars; trailing may end a trade before the price target, so target
comparison is not isolated from existing protection. Effective stop and
target are **frozen once** on each entry.

### Identical portfolio / costs
USD 10,000 initial cash; no leverage; 60% gross exposure bound;
new entry 29% prior-close equity; two-bar reentry confirmation;
8 basis points fee per fill and 5bps one-sided slippage.
No parameters chosen from the inspected PnL. Every arm reruns the
full path-dependent capital and fee/slippage accounting, not single
trade PnL reweighting. Funding bounds from the actual archived
historical rates include final open positions, subject to settlement
timing and the constant-entry-notional approximation.
No actual order-book depth, latency, fill or realized venue impact.

### Frozen selector performance cache is independently verified
Since every arm alters exits **but not the signals**, the original
Strategy A time-indexed selected symbols are reused. These are
the actual decisions calculated from prior bars on each historical day.
The cache never uses later fills or PnL. A full control replay must
match the original portfolio ending equity, each closed trade
**and every decision trace exactly** before the ATR arms execute.
Any difference fails the entire experiment. This eliminates repeated
signal computations, not chronological portfolio recalculation.

### Evidence and restrictions
Source OHLC and funding snapshots SHA-256, all 12 experiment results,
each entry-time ATR and frozen parameter, insufficient-context cases,
fee/funding bounds, drawdown, turnover and conservative OHLC collision
counts are output in one machine-readable immutable report. Include
losing or nontrading arms. A positive historical result is **not**
proof of a profitable live system.

**Following this step:** run both six-major and broad-21 controls,
nested walk-forward with restricted per-symbol parameter-search budget,
entry regime features from preceding completed bars, fresh untouched
future period, real 5m/1m execution and order book stress and
independent code/data audit before any deployment discussion.
No additional paid resources, no automatic execution and no strategy
promotion. `PROFITABILITY_NOT_PROVEN` remains.
