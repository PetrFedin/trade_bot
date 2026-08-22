# Dynamic Bybit Top-10 research universe and historical diagnostics

## Purpose

The research universe is deliberately dynamic. ASTRA must not trade or research a hard-coded list merely because a coin was liquid or popular in an earlier period.

The pipeline answers four different questions with separate evidence:

1. **What is liquid enough now?** — current Bybit linear instrument + ticker snapshot.
2. **What market regimes has each selected coin lived through?** — full available hourly V5 history within a bounded pagination contract.
3. **How has the fixed ASTRA strategy behaved at execution resolution?** — official Bybit public trade archive aggregated to 5-minute bars.
4. **Which entry conditions repeatedly coincided with better or worse realized outcomes?** — completed-bar condition diagnostics plus non-overlapping walk-forward.

None of these layers authorizes order submission.

## Current Top-10 selection contract

`app/marketdata/bybit_research_universe.py` scans the full `category=linear` universe using cursor pagination because Bybit has more than 500 linear instruments.

A candidate is rejected before ranking when any of the following applies:

- instrument is not `Trading`;
- contract is not `LinearPerpetual`;
- quote or settlement coin is not USDT;
- Bybit classifies the linear product as `commodity`, `stock`, `forex`, or `ETF`;
- base asset is a stablecoin-like USD instrument;
- instrument is pre-listing;
- listing age is below the research minimum;
- current ticker is unavailable or has invalid/crossed top-of-book data;
- 24h turnover is below the configured floor;
- open-interest value is below the configured floor;
- best-bid/best-ask spread is above the configured ceiling;
- absolute current funding is outside the configured sanity ceiling.

Eligible symbols are ranked cross-sectionally rather than by one raw metric:

- 35% 24h turnover percentile;
- 30% open-interest-value percentile;
- 20% inverse spread percentile;
- 15% listing-history percentile.

The weights sum exactly to 1 and are research selection weights, not production trading thresholds.

Default guardrails are intentionally conservative and configurable inside the research-only policy:

- minimum listing age: 90 days;
- minimum 24h turnover: 20m USDT;
- minimum open-interest value: 5m USDT;
- maximum top-of-book spread: 25 bps;
- maximum absolute current funding rate: 1%.

If fewer than ten eligible crypto perpetuals remain, the pipeline refuses to pretend that a Top-10 exists and returns `INSUFFICIENT_ELIGIBLE_SYMBOLS`.

## Full-history market profile

`app/strategy/crypto_market_history_profile.py` uses hourly completed bars to make multi-year history tractable while preserving regime structure.

For every selected symbol it reports:

- first/last available bar and bar count;
- total return over the acquired period;
- maximum close-to-close drawdown;
- mean and mean-absolute hourly return;
- hourly return standard deviation;
- fraction of positive bars;
- current and historical `BULL_TREND`, `BEAR_TREND`, and `RANGE_TRANSITION` states;
- qualifying regime episode counts, duration, average episode return, and positive-episode fraction;
- current EMA/momentum/ATR state.

Across symbols it reports pairwise synchronized hourly-return correlations and highlights the highest and lowest observed correlations. Correlation is descriptive and never treated as causality.

## Strategy-condition diagnostics

`app/strategy/crypto_historical_diagnostics.py` reconstructs every accepted replay signal at its original **decision timestamp**, using only bars completed by that time.

It relates realized trade outcomes to:

- strategy quality score;
- absolute momentum;
- momentum / ATR;
- ATR fraction;
- EMA trend separation in ATR units;
- breakout strength in ATR units;
- one-bar move in ATR units;
- average turnover;
- pre-entry expected net edge;
- symbol and LONG/SHORT side;
- fixed-target versus runner exit mode;
- realized net PnL;
- MFE and MAE in R;
- holding duration and exit reason.

Each numeric feature is also grouped into quantile buckets. Repeated multi-factor patterns are reported with an explicit minimum-sample sufficiency flag, so a one- or two-trade coincidence cannot be promoted as a durable pattern.

The report explicitly sets:

- `causal_claim_allowed=false`;
- `predictive_guarantee_allowed=false`;
- `parameter_retuning_performed=false`;
- `strategy_selection_allowed=false`;
- `strategy_promotion_allowed=false`;
- `demo_activation_allowed=false`;
- `live_activation_allowed=false`;
- `bybit_live_order_routing_allowed=false`.

## One-command research run

Run:

```text
python -m tools.research_bybit_dynamic_top10 \
  --site eu \
  --micro-lookback-days 28 \
  --opening-equity 1000 \
  --output artifacts/bybit_dynamic_top10.json
```

`--site` is an enum, not an arbitrary host. It uses the same audited Bybit regional host set used by the read-only account connection.

The output contains:

1. current dynamic Top-10 with score components and exclusion reasons for all rejected instruments;
2. full hourly history profile for the selected ten;
3. recent official public-trade-archive 5-minute acquisition coverage;
4. fixed-parameter non-overlapping strategy walk-forward;
5. side/symbol/candidate comparison;
6. combined-risk trade-condition diagnostics;
7. current turnover/OI/spread/funding snapshot for each selected coin;
8. explicit evidence gaps still preventing a claim of complete derivatives-history attribution.

## Evidence still required

The next research-hardening stack should join the following historical derivatives series to each signal timestamp:

- open interest history;
- long/short account ratio;
- funding history charged through each actual replay holding interval;
- where available, liquidation / market-stress context.

Historical order-book depth is not reconstructable from ordinary V5 kline history, so current spread/depth must not be represented as if it were known for every historical bar.

## Real-account boundary

The real Bybit account connection from PR #50 remains a separate **read-only** observability plane. The dynamic research pipeline needs no account secret and adds no order method.

The canonical product runtime remains demo-only and still requires:

```text
ASTRA_ENV=demo
MAINNET_ENABLED=false
BYBIT_REST_URL=https://api-demo.bybit.com
```

A Top-10 ranking or a historically profitable bucket is therefore evidence for research and manual review, not permission for ASTRA or an AI agent to submit real-money orders.
