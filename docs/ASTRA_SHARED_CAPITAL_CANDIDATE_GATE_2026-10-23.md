# ASTRA / Shared Capital Candidate Gate — 23 October 2026

**Scope:** Research-only. An independent replacement for misleading
per-symbol-account aggregation, not a production strategy promotion.

## Evidence classification

| Component | Admission status | Capital basis |
|---|---|---|
| Legacy Strategy A | Existing frozen research; unchanged | As previously audited |
| Independent 210-coin study | REUSED_HISTORY / simulated | One USD 10k account **per symbol** |
| Shared capital candidate study | NEW RESEARCH / simulated | **One** USD 10k ledger for every symbol in universe |
| Current public Bybit depth | Separate 2026 snapshot | Indicative one-time 10-level public liquidity |
| Verified settlement Mark Price & real account funding | BLOCKED | Not available |
| Order book historical queues and authenticated fills | BLOCKED | Not available |
| Untouched future holdout | BLOCKED | Not yet possible on prior inspected dates |

## Architecture and execution clock

Source SHA per original Bybit UTC daily candle, complete 981-day intersection
across 21 coins → prior-day close signals → next UTC daily open → rank eligible
coins before knowing current-day high/low/close → assign a single cash book
and two position slots → open positions with explicit fee and slippage debit
→ evaluate stops, profit targets and exits later in the daily candle
→ close, book realized P&L and mark any remaining positions at UTC close
→ compare to the same two-position BTC/ETH reference portfolio.

Never finance opening orders using same-day exit proceeds. At an opening,
compute the current portfolio marked at open. Target model allocation is
29% of total opening equity per new position, maximum two positions,
maximum 60% opening gross exposure. Spend cannot exceed available cash.
Quantities are fractional and NOT venue-rounded — historical instrument
filters cannot be reconstructed from today's filters without historical
metadata. Signal priority is fixed a priori: lexical order or descending
previously completed 20-day return, with lexical symbol tie-break.
Neither priority uses the day's future high/low/close.

The 60% bound is an ENTRY gate, not an automatic forced liquidation. Price
changes can increase the closing marked exposure above 60%; the study
records this separately to avoid incorrectly alleging a constant
continuous exposure limit. A new entry is blocked until permitted again.

Long only, unlevered, opening equity USD 10,000, no margin or borrowing,
3% stop, 6% target, 8-day maximum holding in candidate rules. Colliding
stop/target in one daily candle → stop first. Gap-through protective stop
uses next open, not the desired stop price. The separate BTC/ETH benchmark
buys each once at the same initial signal date and closes at archive end,
with the same two-sided cost schedule and shared cash. Benchmark is
IDENTICAL for 6- and 21-coin universes; it is a two-position comparison,
not the previous individual-coin BUY_HOLD_29PC average.

## Research grid

- Four fixed alternative arms: BREAKOUT, IMPULSE, TREND_PULLBACK, MEAN_REVERSION.
- Each alternative receives two fixed, nonoptimized signal rankers.
- One fixed shared-cash reference: BUY_HOLD_BTC_ETH.
- Two previously defined hypothetical fee/slippage pairs per fill (bps):
  8/5 and 10/10.
- Two equal-date universes: SIX and BROAD21.
- (4×2+1)×2×2 = **36** complete shared-cash historical models.

Each model contains chronological decision trace, rejected signals and reasons,
all closed trades with Decimal gross/fee/slippage/net, daily free cash,
end-of-day marked equity, observed entry/closing exposure, closed-trade
win/loss count, peak-to-trough daily-close drawdown and explicit original
source digests. Accounting invariant: final flat equity minus USD 10,000
equals sum of realized net results within strict numeric tolerance.
The all-or-none book admission on the separate snapshot NEVER approves
historical execution. Neither dataset contains verified account funding.

## Offline terminal

The professional ASTRA research HTML contains three deliberately separate
surfaces: historical **independent coin** returns (210), historical **common
cash** returns (36), and **current public depth** quotes (144). A separate
source-of-truth operator-gate page is bundled next to the research terminal;
neither document has a trading action, order API call, remote asset, artificial
live ticker, predictive P&L, invented confidence score or authentication
privilege. The capital graph uses actual audited daily model cash values;
the comparison shows drawdown, rejected entries, commissions and a fixed
BTC/ETH benchmark. Responsive mobile tables disclose field names in compact
cards. The existing muted editorial system, native keyboard-focusable
controls, typography hierarchy and visible danger/positive states are reused.

The renderer rejects mismatched SHA-identical inputs, altered output,
missing 36-model scenario matrix, false permissions, discrepant original
daily-source hashes/time windows across independent and shared studies,
false funding verification and a current book falsely advertised as
historic. CI recreates all 3 original studies and compares before
publishing an immutable no-overwrite standalone HTML.

## Explicit open issues / no claims of profit

This does NOT use actual fill records, exchange-signed source responses,
exact one-minute settlement Mark Price, account funding, account-specific
maker/taker tiers, derivatives margin/liquidation, queue position, latency,
partial fills, cancel risk or historical limit order book. Daily OHLC is
insufficient to prove the real intrabar sequence, even under stop-first
conservatism. Previously inspected 2023–2026 history is not untouched OOS.
Competition for cash is modeled, but real exchange order eligibility and
historical capacity remain unqualified.

No approval to change Strategy A, OMS, demo/live order routing, production
hosting or release gates. **PROFITABILITY_NOT_PROVEN / Production HOLD.**

## Technical next gate

1. Qualify detached SHA and source provenance of contiguous 1m Mark Price,
   funding settlement timing and account-ledger reconciliation.
2. Validate actual account fee tier, instrument minQty/qtyStep history and
   realistic market microstructure; retain missing history as missing.
3. Separate out-of-sample time protocol, repeated walk-forward windows,
   market regimes, PnL uncertainty and trade log audit.
4. Paper-trading on a fully isolated limited account only after independent
   approval; never silently promote a historically selected configuration.
