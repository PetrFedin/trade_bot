# ASTRA: hourly collision replay and signed daily funding cash feedback

Date: 2026-10-15. Scope: LOCAL RESEARCH, NOT GITHUB CI OR PRODUCTION.
Base: `research/full-portfolio-hourly-replay-20261015`, exported commit `1447988dad25229042ac9a8eb1ff1e446b5ba54c`.

## What was actually completed

The previous work had created an export-only PR #324 workflow. This package
contains the actual local implementation, retained source, a patch, targeted
regression tests and full economic results. It does not claim that these
changes have been uploaded to PR #324. Connector discovery timed out.

The original daily-clock portfolio remains the independent control. A narrowly
scoped optional callback refines only a reached daily stop/target collision,
using a hash-checked, UTC-complete hourly case that exactly reconciles to raw
daily OHLCV including fractional volume. No high/low path is invented.
Daily trailing state is held constant inside that day's hourly scan. There is
no intraday re-entry or reinvestment, no strategy parameter search, no leverage
change, and no broker interface. Missing sources stay stop-first and are flagged.
All dynamically reached collisions in these completed runs were source-covered.
The retained archive has 40 case-days / 960 hours, including three extra
stress-case days (INJ 2023-10-19, XRP 2024-12-17, BTC 2025-01-07).

An additional research-only daily funding hook now books signed debits and
credits into cash AFTER that day's trades and BEFORE the next daily sizing.
Definite negative funding rates are credits, not silently truncated to zero.
Uncertain boundary participation is tested under both UPPER_COST and LOWER_COST.
These names describe the fixed-notional event assumption, not guaranteed or
statistical bounds for the resulting dynamic portfolio. Legacy historical
outputs and the original funding helper are retained, not rewritten.

## Primary numerical results

Every number below is cumulative simulated return over its stated window, NOT
annualized return, actual account PnL, a return forecast or a clean holdout.
Starting capital is 10,000 quote units (USDT); stablecoin FX/depeg is not modelled.
Fee is per fill; slippage is per side. UPPER_COST includes ambiguous payments
and excludes ambiguous receipts; definite receipts remain in the balance.

| Window | Fee / slippage (bps) | Original daily stop-first | Hourly-refined |
|---|---:|---:|---:|
| six_full_history to 2026-10-08 | 8 / 5 | -1.8074% | +10.9780% |
| six_full_history to 2026-10-08 | 10 / 10 | -11.0616% | +1.0305% |
| six_matched_window to 2026-06-14 | 8 / 5 | -0.1759% | +12.8220% |
| six_matched_window to 2026-06-14 | 10 / 10 | -8.7724% | +3.6310% |
| broad21_matched_window to 2026-06-14 | 8 / 5 | -20.5061% | -3.6555% |
| broad21_matched_window to 2026-06-14 | 10 / 10 | -28.7484% | -13.5952% |

The exact machine-readable results include all 36 ledgers: 3 historical
window/universe configurations x 2 fee/slippage combinations x 2 exit models
x 3 funding modes (none, upper cost, lower cost). No negative result is hidden.
The six-full-history window is 2023-10-09--2026-10-08. The two genuinely
matched windows are 2023-10-09--2026-06-14; entries begin after warmup on
2023-10-17. TON funding data ends at 2026-06-15 08:00 UTC, so the incomplete
2026-06-15 day is excluded from BOTH matched-universe comparisons. No funding
rate was imputed, and the date was not chosen for favorable PnL.

## Independent arithmetic and tests

76 focused tests passed, covering old portfolio semantics, daily/hourly exits,
raw source checks, positive/negative funding, unknown boundary participation,
open-at-cutoff positions, source gaps, duplicate events, bad identifiers,
nonfinite values, insufficient cash, no-op parity and later position sizing.
The omitted original `research/ohlcv_exit_contract_v1.json` was restored from
its previously retrieved content and verified against Git blob SHA
`e91f7ade9a307bffdd24e7a492bd0624ff1553df`; no contract was invented to pass tests.

A second arithmetic implementation re-read raw rates and daily closes and
reconstructed each ledger independently from entries, exits, fees and funding.
It checked 9,084 closed-trade records,
23,988 funding-event records and
36,384 daily equity records across the
36 model replays. These counts include repeated history across scenarios;
they are NOT counts of independent real-world transactions.

Two complete executions produced identical report dictionaries and identical
content digest `b6f5e1701bfd4068b82dce70c59150202c76f3238ee44d502e54d954807ac861`. Four deliberately corrupted but
REHASHED reports (equity, funding total, fees, missing event) were rejected by
the independent verifier. This is arithmetic validation, not external review.

Local runtime: Python 3.13.5, pytest 9.0.2, cryptography 46.0.4.
Canonical CI requests Python 3.12 and the supplied dependency lock. Ruff was
not installed; its pinned installation attempt failed due to unavailable DNS.
Syntax and 100-column checks passed, but neither Ruff nor the full canonical
locked environment has been verified here. The updated exact-head workflow
is included as a proposed patch and has NOT been run on GitHub.

## Important limits that still control the verdict

Funding is not exact venue settlement: the event source has rates and timestamps
but not historical mark prices. This implementation uses entry execution
notional, does not narrow funding participation to observed first-touch hours,
and books cash daily rather than at actual intraday funding instants. It cannot
prove intraday solvency, liquidation behavior, or derivatives margin truth.
It preserves the existing fully-paid, long-only research ledger, not an actual
Bybit Unified Trading Account. Exit triggers still inherit fee-capitalized
average cost from original Strategy A; venue trigger semantics need a separate
test rather than silently changing the control.

Official Bybit funding documentation consulted on 2026-10-15:
`https://www.bybit.com/en/help-center/article/Funding-fee-calculation`
It specifies funding based on quantity times MARK PRICE, signed payments between
longs and shorts, variable intervals, and uncertain participation around a
settlement boundary. That is why these cashflow outputs are labelled models,
not exchange-account facts. The primary economic result is sensitive to assumed
fees, slippage, execution order, source coverage and previously inspected history.

A SHA check verifies retained bytes, not exchange authenticity. The daily-close
drawdown omits intraday losses. The many prior trials prevent describing these
samples as untouched out-of-sample. Source data is historical and exploratory.

## Priority queue after this package

P0: apply/rebase the patch onto the actual branch, run the full exact-head locked
CI, resolve source/dependency chain and obtain independent review. The relevant
branch state has NOT been refreshed because GitHub timed out. Do not merge into
main or close dependent PRs based solely on these local tests.

P0: acquire and validate timestamped historical MARK prices and contract rules,
reconstruct actual position-exit time intervals, then implement event-time
funding/solvency checks without replacing missing data by zero or a future price.
Keep the current daily cash-feedback mode as a named comparison, not venue truth.

P1: run same-period per-coin and regime comparisons on the corrected engine,
retaining all failed ATR and impulse hypotheses. Freeze the next rule and search
budget BEFORE new evaluation; do not select a joint portfolio from full-history
winners. Exact fills, capacity and a fresh independently sourced holdout remain
separate gates. Positive model return does not authorize trading.

Verdict: PROFITABILITY_NOT_PROVEN. No live trading, production or paid resource
change was made in this local pass. Remote upload and canonical CI are unverified.
