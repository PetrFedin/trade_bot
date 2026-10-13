# ASTRA: Symbol-specific exit experiment v1 (2026-10-13)

**Status:** DRAFT research, main and live execution unchanged.
**Verdict:** `PROFITABILITY_NOT_PROVEN`.

## Thesis to test (NOT a finding)
The uniform 2% stop and 4% take-profit rule is not established as optimal
for every symbol. Test independent per-symbol alternatives while keeping
entry selection, cash management, leverage and total portfolio exposure fixed.

## Predeclared matrix before looking at arm results
Fixed universe: BNBUSDT, BTCUSDT, DOGEUSDT, ETHUSDT, SOLUSDT, XRPUSDT.
Exact original full six-symbol Bybit daily archive and historical funding
settlement snapshots; all dates were previously inspected.

| Arm | Only changed parameter for that coin | All five other coins |
| --- | --- | --- |
| STOP_1P | Stop 1% instead of 2% | Shipped rules |
| STOP_3P | Stop 3% instead of 2% | Shipped rules |
| TARGET_3P | Target 3% instead of 4% | Shipped rules |
| TARGET_6P | Target 6% instead of 4% | Shipped rules |

Trailing activation remains 2%, trailing drawdown 1.5%, maximum holding
10 daily execution bars. **The target-6% arm is not guaranteed to reach
the target because trailing protection can activate earlier**. The
twenty-four variants are competing experiments, not twenty-four
independently validated strategies.

Initial capital USD 10,000; nonleveraged positions, 60% total gross
exposure cap, each new position targeted at 29% prior-close equity,
2-of-N selector, two-bar reentry confirmation, taker fee 8bps/fill,
5bps/side conservative fixed slippage. No new paid resources.

### Accounting and evidence
`tools/per_asset_exit_policy_matrix.py` calculates every full portfolio
counterfactual *separately* from an unchanged Strategy A replay:
entries, future cash, allocation, selection churn, hard stop/target,
gross->slippage->fee->net conservation, unrealized marked equity,
closed trade count, drawdown and turnover. Historic provider funding is
bounded for each closed trade and final open position using **constant
entry notional** and intraday uncertain timing. Report a funding-adjusted
interval, not a falsely exact realized settlement.

The experiment identifies its input historical CSVs and funding CSVs by
SHA-256. Source data was already inspected and is not independent OOS.
Conservative daily stop-first order remains in place wherever both
thresholds are touched in one candle. Conditional first-touch studies
(PR #313/#314) cannot be substituted for a full path-dependent replay.

**No historical winner is automatically selected, no broker/OMS/live
permission is enabled, and a positive result does not prove tradable edge.**

### Follow-through after CI qualification
1. Check exact-head GREEN, source counts, 256-control conservation and
   regression parity when overrides are absent.
2. Publish the *entire* 24-arm table including losses and zero trades.
3. Repeat on the exact common 21-asset portfolio; do not cherry-pick
   successful instruments on the 6-asset sample.
4. Build ATR-normalized per-symbol stop/target from previous completed
   candles only, with exit-policy frozen at each entry.
5. Add prior-bar regimes (trend/range/high-low vol) and per-symbol momentum,
   relative volume and liquidity constraints. NO current-bar lookahead.
6. Preregister bounded parameter search and use **nested** chronological
   walk-forward. Optimize inside training, evaluate on strictly later
   validation windows. Retain all tried arms to quantify multiple-testing
   and selection bias.
7. Compare net return, drawdown, turnover, funding bound, capacity, number
   of trades, stability by regime, BTC and capital-matched benchmarks.
8. Only after candidate freeze, require newly arriving untouched market
   period and independent reviewer. Research alone cannot authorize
   paper/demo/live broker activity.
