# Bybit full-period 5-minute evidence backfill

## Why this layer exists

The current dynamic Top-10 research pipeline has two different history depths:

- the broad price/turnover regime profile is hourly and reaches back toward instrument launch;
- the execution-grade 5-minute replay defaults to `micro_lookback_days=28`.

Therefore the repository must not describe the existing 5-minute strategy evidence as
"the whole period". v113 creates the authoritative incremental history needed to remove
that limitation without redownloading years of raw trades on every research run.

## Coverage contract

For every symbol in the current qualified Top-10 universe, expected archive coverage is:

```text
UTC launch date of that instrument
through
yesterday UTC
```

Each `symbol × archive_date` is classified as exactly one of:

- complete — an official Bybit public trade archive was aggregated to validated 5m bars
  and committed to the append-only store;
- blocked — the latest archive attempt was unavailable and has not reached `retry_after`;
- pending — not yet complete and currently eligible for acquisition/retry.

The planner reports expected, complete, blocked, and pending day counts by symbol and for
the complete Top-10 set. `full_period_claim_allowed=true` only when every expected day for
every selected symbol is complete. An unavailable archive never counts as coverage.

## Bounded incremental acquisition

`python -m tools.backfill_bybit_full_period_5m` performs one bounded iteration:

1. resolve the current qualified dynamic Top-10 from the selected regional Bybit public
   V5 host;
2. read existing v113 coverage from PostgreSQL;
3. plan the oldest currently eligible missing `symbol × day` work items;
4. download each day independently from Bybit's official public trade archive;
5. aggregate raw trades into 5-minute OHLCV/turnover bars in memory;
6. persist immutable bars and a complete-day fingerprint, or an explicit unavailable
   attempt with a retry boundary;
7. rebuild and print the coverage manifest.

The raw trade archives are not committed to GitHub. They are a reproducible source used
to construct the authoritative PostgreSQL 5m history.

Single-symbol acquisition is intentional: one missing archive must not fail or contaminate
all other Top-10 symbols.

## PostgreSQL v113

`migrations/v113/001_bybit_full_period_5m.sql` creates:

- `astra_bybit_5m_archive_day_v113` — immutable complete/unavailable archive attempts;
- `astra_bybit_5m_bar_v113` — immutable 5-minute bars keyed by symbol and start time.

A complete day stores a SHA-256 fingerprint over the canonical ordered bars. Repeating an
identical day is idempotent. If the same symbol/timestamp or complete archive day later
contains different OHLCV/turnover values, persistence fails rather than rewriting market
history.

Both tables reject UPDATE and DELETE.

## What this PR does not claim yet

v113 creates and validates the full-period 5m evidence substrate. It does not claim that
the backfill has already reached 100% in an environment where the historical downloads
have not actually run.

It also does not yet claim full-period OI, account-ratio, funding, liquidation, or order-
book coverage. Those layers have different source availability and must carry independent
coverage manifests. Historical evidence must use the intersection of data that was truly
available at each decision time.

Once v113 reaches complete coverage for a frozen research universe, the next research
step is to run the unchanged fixed strategy over these bars and build a full-period
signal/outcome population, then join point-in-time derivatives context only where its own
coverage is validated.

## Safety boundary

This is market-data acquisition and evidence infrastructure only:

```text
trade_actionable=false
demo_activation_allowed=false
live_activation_allowed=false
bybit_live_order_routing_allowed=false
```

No order-create, amend, or cancel surface is introduced. Full-period associations remain
historical evidence, not causal proof or a guarantee of future profit.
