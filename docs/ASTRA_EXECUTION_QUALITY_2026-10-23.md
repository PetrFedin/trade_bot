# ASTRA — execution quality and coin alternatives (2026-10-23)

Research-only addition, stacked on the audited Research Terminal PR #336.
No Strategy A signal, OMS, order mutation, demo/live authority, risk limit,
broker credential, deployed site, or paid resource has been changed.

## A. Current read-only Bybit visible-liquidity experiment

Source: six public LINEAR books (BTC, ETH, BNB, SOL, XRP, DOGE)
plus their public instrument filters, queried through the Bybit public
read-only plugin around 2026-10-22 UTC. One retained JSON source
contains ten visible bid and ten visible ask levels per coin. A Git SHA
anchors retained bytes; the public response is NOT exchange signed.

New pure decimal engine: app/execution/market_depth_quote.py
New reproducible audit: tools/depth_execution_research.py

Validation rejects: inactive or mismatched instrument, crossed/unsorted
or duplicated book, invalid tick size, nonpositive level, stale/future
timestamp, invalid minQty/qtyStep/maxQty/minNotional, nonfinite number,
overlarge fee and untenable book quantity. Qty rounds DOWN to instrument
step before any estimate. A quote fails if visible levels cannot supply
the full order; any partial walk is only displayed liquidity, NOT a fill.

Scenario grid: 6 coins × 3 hypothetical capital amounts (10k, 100k, 1m
USDT) × 2 assumed fee cases (8/10 bps per fill) × 2 quantities
(full displayed depth / 50% depth haircut) × 2 market directions
(BUY/SELL) = 144 indicative calculations. Each model order requests
29% of that hypothetical capital, for ONE symbol independently.

Output: visible quantity, deficit, rounded requested quantity,
hypothetical VWAP, adverse spread/depth crossing vs midpoint, fee,
and all-or-none status. NEVER claim executed/fillable with certainty.
The 50% haircut is an explicit stress assumption, NOT observed depth.
Only ten publicly exposed levels were supplied. Account maker/taker
fees, latency, queue, cancel risk and actual market impact unknown.
Zero live/demo trading permission.

## B. Four independently fixed strategies plus a benchmark

New script: tools/independent_coin_strategy_lab.py.

On exactly matched 21-coin DAILY Bybit OHLCV archives,
2023-10-09 through 2026-06-15 (981 synchronized UTC days):

- BREAKOUT: 20-day high breakout with SMA10>SMA50 and volume filter.
- IMPULSE: day-on-day price rise >=4% with close above SMA50.
- TREND_PULLBACK: price above SMA50 but >2% under SMA10 in pullback.
- MEAN_REVERSION: price >6% under SMA20 and closing down.
- BUY_HOLD_29PC: buy-and-hold of one 29% cash position as reference.

Signal evaluated AFTER close of UTC day T. Entry at the next day OPEN.
Long-only, cash-funded independent $10k model per symbol, 29% per
position, 3% stop, 6% target, at most 8 days held. Both protective and
target touched in a day -> STOP FIRST. Gap-through-stop -> next actual
daily open with sell slippage. Entry/exit charge fees and slippage,
full trade-level accounting with Decimal, daily equity and drawdown.

21 coins × 5 frozen arms × 2 fee/slip assumptions (8/5 and 10/10 bps
per side) = 210 separate historical experiments. Six-coin and 21-coin
descriptive summaries compare identical periods. Every coin uses a
SEPARATE $10k model account, NOT 21 simultaneous real accounts and not
one common-capital constrained portfolio. Do not add or market average
returns as real strategy/portfolio P&L.

The source was already inspected; neither future holdout nor predictive
profitability has been demonstrated. This is a controlled test of fixed
ideas, not a model-selection or order-authorizing system.

## C. Explicit source and financial completeness boundary

- Historical source SHA-256 pinned for every daily price/volume file.
- 21-coin matched time spine, reject duplicate or absent UTC days.
- Synthetic lookahead/truncation, stop-first, gap, cash/fees tests.
- Independently verify every deal's gross - slippage - fees = net
  and 10000 + sum(deal net) = final standalone-model cash.
- Historical raw daily-volume capacity proxy recomputed separately
  for 6 and 21 coins. Full-day turnover is NOT a historical order book.
- Funding is NOT incorporated in new per-coin strategy returns.
- Historic one-minute Mark Price, exact settlement evidence and actual
  account funding statement remain UNQUALIFIED. Earlier public Bybit
  collector HTTP 403 is a source blocker, not a reason to fabricate data.
- Today's public orderbook is NOT applied to the 2023–2026 backtest.
- Fee hypotheses are NOT a verified actual Bybit account fee tier.
- No leverage, margin/liquidation, signed exchange fills, maker queue,
  liquidity replenishment, asynchronous cancellations or limit-order
  adverse selection are modeled.
- Promotion/release/demo/live permissions remain false.
- PROFITABILITY_NOT_PROVEN.

## Next independent admission

Retain raw one-minute Mark Price archive with a detached source pin,
validate 1m chronology and funding intersections, separately qualify
account funding ledger and actual maker/taker tier. Only then compare
derivatives strategy cashflows. Require an uninspected future test
period and a shared-capital portfolio replay before considering live
execution authority. Research output must never issue or authorize
orders itself.
