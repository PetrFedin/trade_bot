# Bybit opportunity registry

## Purpose

The registry turns the qualified public Bybit universe selector into a timestamped, append-only
operating history. It is designed to answer two different questions without mixing them:

1. which USDT linear perpetuals are currently the strongest market-quality research universe;
2. which of those symbols later receive a qualified LONG/SHORT strategy signal.

This change unit implements question 1 only. A high registry rank is **not** an instruction to buy
or sell.

## Ranking population

The registry reuses the same eligibility and scoring policy as the qualified dynamic Top-10
selector. The default eligibility guardrails remain:

- `Trading` linear perpetual;
- USDT quote and settlement;
- crypto base asset, not stablecoin/non-crypto linear product;
- no pre-listing or scheduled delivery/delisting;
- at least 90 days of listing history;
- at least 20m USDT 24h turnover;
- at least 5m USDT open-interest value;
- spread no wider than 25 bps;
- absolute funding rate no larger than 1%;
- finite, non-crossed top-of-book and finite market fields.

Eligible symbols are scored cross-sectionally using the existing fixed weights:

- 35% 24h turnover percentile;
- 30% open-interest-value percentile;
- 20% inverse spread percentile;
- 15% listing-history percentile.

The first 10 symbols are `Top-10`. Up to 50 eligible symbols are retained as the extended watchlist.
Scores are calculated against the full eligible population, so expanding the retained watchlist does
not change the Top-10 scoring formula.

## Safety semantics

Every snapshot and candidate is fail-closed:

- `signal_side=UNASSIGNED`;
- `trade_actionable=false`;
- `strategy_promotion_allowed=false`;
- `live_activation_allowed=false`;
- `bybit_live_order_routing_allowed=false`.

The PostgreSQL schema repeats these constraints as database `CHECK` constraints. Snapshot and
candidate tables are append-only through `BEFORE UPDATE OR DELETE` triggers. There is no order,
cancel, amend or authenticated trading method in the registry client/store/CLI.

This separation is deliberate. The next evidence layer may attach a strategy-side score and
historical regime statistics, but it must do so without silently turning market suitability into a
trading decision.

## PostgreSQL source of truth

Migration:

```text
migrations/v110/001_bybit_opportunity_registry.sql
```

Tables:

- `astra_bybit_opportunity_snapshot_v110` — one canonical snapshot per host and observation time;
- `astra_bybit_opportunity_candidate_v110` — ranked candidate rows for each snapshot.

The snapshot ID is SHA-256 over canonical snapshot JSON. Repeated persistence of the same host,
timestamp and content is idempotent. Different content for the same host/timestamp is rejected.

Run the migration once during deployment before enabling scheduled PostgreSQL persistence.
The 10-minute GitHub workflow intentionally does not execute DDL on every refresh.

## Public snapshot command

No API key is required for market-universe capture:

```bash
python -m tools.snapshot_bybit_opportunity_registry \
  --site eu \
  --registry-limit 50 \
  --output artifacts/bybit-opportunity-registry.json
```

Optional PostgreSQL persistence reads the DSN from an environment variable and never prints the
secret:

```bash
export BYBIT_OPPORTUNITY_DATABASE_DSN='postgresql://...'
python -m tools.snapshot_bybit_opportunity_registry \
  --site eu \
  --registry-limit 50 \
  --output artifacts/bybit-opportunity-registry.json \
  --persist-postgres
```

For a first deployment only, `--migrate-postgres` can be added together with
`--persist-postgres`. Normal recurring runs should not execute the migration.

## Refresh cadence

`.github/workflows/bybit-opportunity-registry.yml` has a `*/10 * * * *` schedule and a manual
`workflow_dispatch` path. Scheduled GitHub Actions are best-effort and should be treated as an audit
and fallback capture path, not a hard real-time clock. The production runtime should call the same
CLI/service every 10 minutes and persist to PostgreSQL.

If the repository secret `BYBIT_OPPORTUNITY_DATABASE_DSN` exists, scheduled runs persist the
snapshot. Without that secret they still produce a public-data JSON artifact. The optional repository
variable `BYBIT_RESEARCH_SITE` controls the audited regional Bybit public host.

## Data lineage

Each stored row carries:

- exact observation timestamp;
- source host;
- full selector policy;
- eligible/source counts;
- Top-10 completeness;
- ranked candidates and score components;
- liquidity, OI value, spread, funding and 24h price change;
- exclusions and blockers;
- explicit non-trading safety flags.

This lets later diagnostics compare rank changes to subsequent market behavior without rewriting the
historical record.

## Next evidence layer

The registry is intentionally ready for, but does not yet claim, the following fields:

- fixed-strategy LONG/SHORT signal quality;
- price/EMA/ATR and volatility regime;
- point-in-time OI delta;
- account-ratio crowding;
- prior funding and reconstructed funding economics;
- liquidation/stress context where reconstructable without lookahead;
- execution economics;
- historical matrix by symbol, side and regime with PnL, profit factor, win rate, MFE, MAE,
  drawdown and sample sufficiency.

Associations in that future matrix are evidence about historical conditions, not proof of causality
or a guarantee of future profit.
