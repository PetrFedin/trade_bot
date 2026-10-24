# ASTRA — Coin Selection, Signal-to-Trade Funnel — 24 October 2026

**Engineering evidence, not an investment performance claim.** This source-bound
report improves the decision analysis of the existing shared-capital simulator.
It does not modify Strategy A, alter venue integration, issue orders, promote
historical winners or publish an unattended live deployment.

## Audited source boundary

This study accepts exactly the three original, byte-reproducible historical
research documents:

- 36 common-$10k capital portfolios, pinned SHA-256
  `e2f8c37d5c6df948dccfbb054ad2941a443ba2b5b3015bdfadfee44ee82f9613`
- 64 fixed BTC entry-gate variants, pinned SHA-256
  `04ae594ea9d67cdb85363910761de86f91fc9b0b779904c0914d372298b5b75c`
- 36 previous-close BTC regime/tail-risk diagnostics, pinned SHA-256
  `f29cd1a860d7df82c6e72ffa895d4e5daafb7be40ece08bd9171674024761087`

The audit regenerates all reports from historical Bybit daily files in CI
and validates the retained raw BTC archive against its full byte SHA-256.
It requires 981 synchronized BTC days and 930 daily portfolio marks.

## Key discovery: order of capital admission changes outcomes

Example: original 8bps fee + 5bps modeled slippage on each side,
long-only BREAKOUT with fixed alphabetic signal priority:

| Metric | SIX | BROAD21 |
| --- | ---: | ---: |
| Eligible prior-day signals | 206 | 620 |
| Accepted new positions | 147 | 288 |
| Rejected for two occupied slots | 59 | 332 |
| Historical model return | +3.50% | -33.91% |

The broader model rejected 332/620 ≈ 53.5% of eligible **signals** because
the two slots were already occupied; this is not an execution reject
from the exchange. A skipped signal has no actual modeled position and
no known counterfactual P&L. Assigning it the next candle's hypothetical
return without re-running the entire common-cash portfolio would be
look-ahead and capital double counting.

For the original BROAD21 alphabetical BREAKOUT low-cost model, illustrative
per-coin realized model P&L (rounded USDT; only selected, closed trades):
APT -1186.5, ARB -620.7, SUI -606.8, LINK -411.7, NEAR -401.4,
ADA +975.4, BNB +397.4 and DOGE +233.2. These are **descriptive** and
are not a basis for automatically excluding losing instruments.

## New deterministic accounting

For each original/gated model and each eligible instrument:

1. Read the chronological ranked-candidate list from each prior-day signal.
2. Verify every signal belongs in exactly one of accepted or rejected;
   no signal is double counted, no active holding is re-entered.
3. Verify the set of open holdings across each day's entry/exit trace
   and the daily marked portfolio, including same-day entry/stop closures.
4. Audit every closed trade by symbol and exact entry/exit timestamp;
   reject a fill with no corresponding decision or vice versa.
5. Reconcile gross - actual **modeled** fees - **modeled** slippage = net,
   for each trade, each coin and the whole cash account; reconcile free
   cash plus marked holdings to equity each day.
6. Group the **eventual net P&L of accepted trades** by the BTC regime
   known at their *entry*, not by the regime at exit or the day-end
   mark-to-market cash contribution. These are distinct measures.
7. Retain counts by the original rejection reasons: MAX_TWO_POSITIONS,
   ENTRY_EXPOSURE_CAP, MIN_ALLOCATION_NOT_REACHED, BTC_PRIOR_CLOSE_ENTRY_GATE.
8. For 48 fixed universe/arm/cost/gate combinations, compare two historical
   priority rules (alphabetical vs prior 20-day instrument return).
   Full portfolios have path dependence and different opportunity sets:
   this comparison is NOT a paired causal effect or missed P&L.

**100 exact historical portfolio variants:** 36 original + 64 gated.
The BTC/ETH passive benchmark has no gated or alternative-ranking form.
No strategy optimization, signal score fitting, factor weight search or
retroactive selection is applied.

## UI and delivery

A new offline ASTRA **Signal Selection Research** terminal joins the
original execution, risk, and operator pages with internal relative links.
The visible structure is source-driven: filters for SIX/BROAD21, 5 arms
(with fixed benchmark controls), two priority policies, two cost cases,
two gates plus ungated, per-coin ranking and detailed entry-regime,
commission, slip and rejection analysis. Mobile tables are labeled cards;
buttons are native, keyboard-focusable and visibly selected. No remote
assets, realtime indicators or trading action is present.

The HTML is rendered only after independently recalculating the 100-model
funnel from the exact three original reports and the source-matched raw BTC
archive, then embedding a safe static JSON payload with no secret, network,
analytics pixel or buy/order endpoint. The resulting report and exact head
SHA are retained as CI evidence.

## Unresolved / mandatory safety gate

Historical data were already inspected, **not untouched OOS**. True historical
perpetual Mark Price at settlement, real account funding, fee tiers, maker
queue, historical orderbook depth, partial fills, instrument-step history,
liquidation and full venue execution remain unqualified. The ignored-signal
counterfactual is NULL/unknown by construction. A previously winning
coin, filter or entry ranker MUST NOT be auto-promoted from this audit.

`PROFITABILITY_NOT_PROVEN` — `PRODUCTION HOLD` — live trading disabled.
