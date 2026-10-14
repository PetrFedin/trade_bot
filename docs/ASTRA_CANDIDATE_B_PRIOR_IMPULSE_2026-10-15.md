# ASTRA Candidate B — completed-bar impulse exhaustion entry hypotheses

Date: 2026-10-15. Draft, research only. `PROFITABILITY_NOT_PROVEN`.

## Investigative priority

The unchanged six-asset Strategy A portfolio generated 256 closed trades
in the pinned 2023-10-09 through 2026-10-08 daily Bybit archive,
of which 158 were marked as closing on the entry daily bar. The existing
ATR-exit-only variants improved some full-period inspected-history
returns, but the [seven-window nested ATR selection](https://github.com/PetrFedin/trade_bot/actions/runs/38599307148)
underperformed the original benchmark in three windows, equaled it
in three, and improved in only one. This falsified the first
ATR-profile selection *procedure*, not every possible asset-specific
entry hypothesis.

Therefore this lane tests an explicit DIFFERENT question:
**Is Strategy A entering immediately after a completed excessive upside
impulse that raises same-day exit risk?** No observed positive result
may be taken as OOS since the entire archive was previously inspected.

## New entry-only callback

The original synchronized shadow `CrossSectionalPortfolioBacktester`
is extended with an optional `research_prior_entry_veto(symbol,
tuple(prior_completed_OhlcvBar)) -> bool`. On absence it is byte-for-byte
the original Strategy A. It is invoked only on a new entry that
passed original ranking and reentry protection. Rejecting an entry
does **not**:
- change the top-two ranking or force-select the third-ranked coin;
- close an existing held coin (which would conflate entry and exit);
- remove an existing protective hard stop or trailing stop;
- alter leverage, position size, portfolio exposure or funding logic;
- use today's execution candle high, low, close or unrealized PnL.

No ATR history => default allow, with logged missing-history cases.
A veto blocks the proposed entry, leaves the opportunity unfilled and
is logged in `PortfolioEntryBlockReason.RESEARCH_PRIOR_ENTRY_VETO`.

## Fixed experiment budget BEFORE inspecting result

Universe exactly BNBUSDT, BTCUSDT, DOGEUSDT, ETHUSDT, SOLUSDT,
XRPUSDT. Daily ATR14 is the mean of 14 gap-aware true ranges from
the *last fifteen completed bars*, normalized to the *last completed
close*. The trigger is **positive** prior close-to-close price motion
only, no bearish shorting implied.

| Candidate hypothesis | New entry rejected if… |
|---|---|
| PRIOR_1D_ATR_150 | Last completed daily return > 1.50 × prior relative ATR14 |
| PRIOR_2D_ATR_225 | Return over last two completed daily bars > 2.25 × prior relative ATR14 |

Both are predetermined threshold hypotheses, not learned optima.
Each is run independently for ONE of the six symbols, keeping
the other five on exact original Strategy A conditions:
**12 single-symbol arms**. Also run each hypothesis as a combined
all-six filter (**2 joint arms**), in total exactly **14 arms** plus
the original Strategy A control. The 2 combined tests are
predeclared, not synthesized from historical single-coin winners.

## Economic and evidence contract

Re-run the entire path-dependent portfolio for each arm with original
long-only top-two selection, capital USD 10,000, top-two exposure
60% max, individual new position 29% equity allocation,
two eligible bars reentry confirmation, hard stop 2%, target 4%,
trailing activation 2%, trailing gap 1.5%, maximum hold 10 days.
Assume 8 basis points taker fee per fill and 5bps slippage per side.
Original control must match exactly **256 closed trades** and
+0.7515525902% **before** funding. Pre-existing historical actual
funding-rate snapshots estimate uncertain settlement cost bounds.
Do not report fake exact exchange fills, spreads or mark-price
funding. No realized live PnL exists.

For every arm, report ALL losses/wins, full funding-adjusted
return LOWER/UPPER, fees, cash/equity, turnover, drawdown, entries
vetoed, same-entry-bar exits, same-bar stop-vs-target ambiguity,
source price/funding SHA-256 and each prior-close impulse
decision. A reduction in same-day stops can coexist with worse
total PnL by missing profitable trades or simply never trading;
zero-trade arms are explicitly ineligible for strategy promotion.

### Hard gates
Historical 2023-2026 full-period arm comparison is *only discovery*
and multiple testing counts all 14 trials. Do not cherry-pick a
profitable per-asset policy from the full-period observations.
If any fixed hypothesis looks worth challenging further, freeze
its explicit rule SHA and repeat on chronological 420/90/90
nested tests, comparing with *exact same capital, fees, funding,
risk exposure and passive benchmarks*, without reoptimizing on
outer test outcomes. Final independent newly collected 90+ day
post-freeze untouched holdout requires [the temporal admission
contract](https://github.com/PetrFedin/trade_bot/pull/320), true
provider provenance and independent reviewer. No broker, paper,
OMS, production or paid infrastructure is enabled by this study.

## Review checklist
- **Default parity**: None gate / always allow produce identical
  complete Strategy A portfolio decisions and cash entries.
- **Temporal**: callback receives symbol bars ending at most at
  previous day UTC; action cannot see execution-bar OHLC.
- **Isolation**: veto is on new-entry path only, never an exit;
  no reranking, no implicit leverage or replacement.
- **Evidence**: 12 single-symbol and 2 predeclared joint arms,
  rejected-entry count reconciles, negative arms cannot be removed,
  raw six OHLC and six funding digests stored.
- **Result authority**: experimental in-sample observation, no
  optimization, no OOS claim, no automatic strategy promotion.

**Production HOLD** / `PROFITABILITY_NOT_PROVEN`.
