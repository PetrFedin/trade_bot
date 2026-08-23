# Bybit full-period derivatives coverage (v114)

## Purpose

This layer extends the verified full-period 5-minute price archive with the public derivatives context required by the historical evidence matrix:

- open interest (OI);
- long/short account ratio (crowding);
- funding-rate history.

It is a research and evidence-coverage subsystem. It does not create, amend or cancel orders and cannot enable live mainnet routing.

## Why coverage is source-specific

The three public histories do not share one identical lifetime or one identical sampling contract. A single `full_period=true` flag would therefore be misleading.

The implementation keeps two separate concepts:

1. **instrument-lifetime completeness** — the source covers the instrument from its listing time through the latest completed UTC day;
2. **source-available-period completeness** — every day that the official source can legitimately provide has been collected and validated, even when the official source begins after the instrument itself was listed.

The combined evidence matrix may claim full instrument lifetime only when all required sources satisfy the first definition. A separately named source-available common-period matrix may be built later when all sources are complete from their common start.

## Official source boundaries used by the contract

### Open interest

Official Bybit V5 endpoint: `GET /v5/market/open-interest`.

The endpoint supports `5min`, `15min`, `30min`, `1h`, `4h` and `1d`, `startTime`, `endTime`, cursor pagination and up to 200 records per page. Bybit documents the symbol launch time as the historical query limit. v114 therefore sets the OI source start to the instrument `launchTime` and validates an exact 5-minute grid for the requested day.

Reference: https://bybit-exchange.github.io/docs/v5/market/open-interest

### Long/short account ratio

Official Bybit V5 endpoint: `GET /v5/market/account-ratio`.

The endpoint supports a 5-minute recording period, `startTime`, `endTime`, cursor pagination and up to 500 rows per page. Bybit explicitly documents **July 20, 2020** as the earliest query start time. v114 therefore sets:

```text
account_ratio_source_start = max(instrument.launchTime, 2020-07-20T00:00:00Z)
```

For an instrument listed before that date, complete crowding coverage from 2020-07-20 onward does **not** become an instrument-lifetime claim.

Reference: https://bybit-exchange.github.io/docs/v5/market/long-short-ratio

### Funding history

Official Bybit V5 endpoint: `GET /v5/market/funding/history`.

The endpoint supports `startTime`, `endTime` and up to 200 rows. Bybit states that each symbol can have a different funding interval and directs clients to instrument metadata for that interval. v114 therefore treats funding as an event series: a successful bounded query window is stored without inventing a fixed 5-minute expected event count.

Reference: https://bybit-exchange.github.io/docs/v5/market/history-fund-rate

## Backfill flow

```text
one instrument snapshot + one ticker snapshot
-> qualified dynamic Top-10 selection
-> source-specific coverage plan
-> oldest pending source x symbol x UTC day
-> regional public Bybit GET
-> exact-grid audit for OI/account ratio or bounded event-series audit for funding
-> append-only PostgreSQL point storage
-> append-only COMPLETE / UNAVAILABLE day record
-> recompute coverage plan
```

The same instrument snapshot is used for both Top-10 selection and coverage planning so a listing/delisting change between API calls cannot create a split-brain research universe.

## PostgreSQL v114

The authoritative append-only schema separates the different grains:

- `astra_bybit_derivatives_day_v114` — day-level acquisition and coverage evidence;
- `astra_bybit_open_interest_v114` — OI points;
- `astra_bybit_account_ratio_v114` — long/short account-ratio points;
- `astra_bybit_funding_rate_v114` — funding settlement events.

Repeated identical writes are idempotent. A divergent value for an already stored immutable point/day is rejected rather than overwritten.

## Fail-closed rules

A source day is not marked complete when:

- the public request fails;
- OI or account-ratio has a missing expected 5-minute timestamp;
- a duplicate or out-of-range point appears;
- the API window cannot be shown to be complete;
- the stored immutable history conflicts with a repeated acquisition.

Temporary failures are written as `UNAVAILABLE` with a retry time. They are not converted into zeroes, interpolated values or completed coverage.

## Evidence semantics

For old contracts such as those listed before July 20, 2020, a combined lifetime matrix containing account-ratio context cannot honestly cover the entire instrument lifetime because the official crowding source itself begins later. The system must report that limitation explicitly.

The correct hierarchy is:

```text
full price lifetime
-> full OI lifetime where available
-> full funding source history where available
-> account-ratio from documented source floor
-> source-available common-period evidence matrix
-> instrument-lifetime combined matrix only if every required source truly covers it
```

This prevents a shorter derivatives history from being silently presented as an all-history proof.

## Safety boundary

Every v114 artifact remains research-only:

- `trade_actionable=false`;
- `strategy_parameters_changed=false`;
- `strategy_promotion_allowed=false`;
- `demo_activation_allowed=false`;
- `live_activation_allowed=false`;
- `bybit_live_order_routing_allowed=false`.

Historical associations between price/EMA/ATR, OI, crowding, funding, volatility and execution economics are evidence about observed outcomes, not proof of causality and not a guarantee of future profit.
