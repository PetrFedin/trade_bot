# C2A4 — v119 Active Excursion Persistence Preservation Audit

## Purpose

C2A4 extracts only the persistence-safe active-excursion checkpoint/CAS primitive from historical PR #76 onto current canonical `main`. It does not copy the historical strategy, broker, market-data, monitor or observation/finalization builders.

This document is preservation evidence, not broker/Demo/live qualification.

## Source identity

- historical PR: #76 — `Persist canonical Demo runtime state in PostgreSQL`
- exact source head: `84b9965e5128562c49620c6164afe5f43ca343f2`
- historical base head: `755b1dc2d2330b53b3ecc0d32db1cc4eb5e21814`
- current C2A4 base: canonical `main` documentation baseline `2947cbda46e0a6b2e9e79834e43951c11c398cea`
- C2A4 tracking issue: #122

PR #76 changed six files. The only new active-excursion PostgreSQL adapter in that PR was `app/execution/bybit_demo_postgres_excursion_store.py`; the state/store/tracker types it consumed came from the stacked base and therefore require an explicit dependency audit rather than wholesale ancestry adoption.

## Reproducible preservation probe

A temporary GitHub Actions workflow fetched the exact source head and ran, for every audited path:

- `git rev-parse <source_sha>:<path>`;
- `git show <source_sha>:<path>`;
- `sha256sum` over the exact bytes;
- import-line extraction.

Probe run: `33989042381`, job `101367823531`, result `SUCCESS`.

The temporary workflow was deleted after the evidence was recorded and is not part of the C2A4 implementation branch.

## Exact audited blobs

| Path | Git blob | SHA-256 | Classification | C2A4 treatment |
|---|---|---|---|---|
| `app/execution/bybit_demo_postgres_excursion_store.py` | `6d07dc72ef8adb0b2590e4e72ff8d44d53523a79` | `78cbac8a1db07ab53d9fcfd2d8fe695d272d8312e45ec00a8d37d1b7b5d58fd8` | PERSISTENCE, but coupled to mixed domain types | Preserve CAS semantics; rewrite adapter against neutral record contract; remove `migrate()` |
| `app/execution/bybit_demo_excursion_store.py` | `6150773ded4f7a6ae31e05d0499fe4ad4a25b953` | `2b3c95ef3d8209bfeadb461faad5d66d477692ef7d2c36db366f8be0cf82950f` | PERSISTENCE + STRATEGY dependency | Preserve canonical JSON/revision and validation semantics only; do not copy file wholesale |
| `app/execution/bybit_demo_excursion_tracker.py` | `7caca38b668557389b681474fcd54b3bf3b91ad0` | `3180fd34f41d3a986593ef2e00c872597df78fd71eac12d8792e2f6fa814f896` | STRATEGY + BROKER + MARKET_DATA + OPERATIONAL observation logic | Excluded from C2A4 canonical persistence |
| `tests/test_bybit_demo_postgres_runtime_state.py` | `bcf04f415163d38ab75ecf0d742bb21f58a71b5f` | `2450aeb1f2bde85c3684e866edf281b8915bf85c5c7eeda9e64cff828de7335e` | Mixed PERSISTENCE + STRATEGY + BROKER + MARKET_DATA test | Re-express persistence/CAS invariants with neutral fixtures and existing C2A1 runtime role |

The already canonical v119 schema is **not forked or edited by C2A4**:

- `migrations/v119/001_bybit_demo_durable_runtime.sql`
- canonical Git blob: `9a7f8d6eee89d10673b288e0e6a9bfe276494d8a`
- frozen SHA-256 from C2A0: `c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e`

## Import-boundary proof

Historical `bybit_demo_postgres_excursion_store.py` imported:

- `bybit_demo_excursion_store`;
- `BybitDemoTradeExcursionState` from `bybit_demo_excursion_tracker`.

Historical `bybit_demo_excursion_store.py` imported:

- `BybitDemoTradeExcursionState`;
- `CryptoSide` from `app.strategy.crypto_perp`.

Historical `bybit_demo_excursion_tracker.py` imported:

- `BybitDemoPosition` — BROKER/EXECUTION state;
- `BybitDemoTradeMonitorResult` — OPERATIONAL CONTROL;
- `BybitDemoMarketQuote` — MARKET_DATA;
- `CryptoSide`, `CryptoTradePlan` — STRATEGY.

Therefore copying the old typed store dependency closure would reintroduce strategy/broker/market-data ancestry into canonical infrastructure. C2A4 explicitly rejects that design.

## Historical semantics preserved

C2A4 preserves the following behavior:

1. one singleton checkpoint named `ACTIVE`;
2. exact `ASTRA-DEMO-...` entry orderLinkId identity;
3. deterministic state JSON using sorted keys, compact separators and ASCII JSON;
4. Decimal values persisted as strings;
5. revision SHA-256 over canonical JSON of `{entry_order_link_id, state}`;
6. initialize with insert-if-absent and fail closed if a checkpoint already exists;
7. load validates safety columns, payload type, revision and state;
8. save is compare-and-swap on checkpoint name + exact entry identity + expected revision;
9. clear is compare-and-swap on checkpoint name + expected revision;
10. stale/concurrent revision rejection;
11. cross-trade orderLinkId rejection;
12. diagnostics-only semantics;
13. exit-threshold retuning disabled;
14. strategy promotion disabled in the neutral record contract;
15. live/mainnet routing disabled.

## Historical behavior deliberately not preserved in the runtime adapter

The historical PostgreSQL adapter exposed `migrate()`. C2A4 does not: schema migration remains bootstrap authority, already separated from the long-running runtime credential by C2A1.

C2A4 also excludes:

- creating excursion state from a `CryptoTradePlan`;
- observing prices/positions;
- MFE/MAE calculation from live quotes;
- finalization from trade-monitor results;
- broker or exchange calls;
- order submit/cancel/amend/protection/close;
- ARM/HALT;
- operator approval;
- strategy selection, sizing or risk changes;
- mainnet routing.

## Canonical dependency boundary

C2A4 may depend only on:

- Python standard library;
- optional `psycopg` at the PostgreSQL adapter boundary;
- the new neutral excursion persistence record/codec;
- the already-qualified C2A1 PostgreSQL runtime-role/schema contract in tests/preflight.

It must not import from `app.strategy`, `app.marketdata`, broker clients, trade monitors or order mutation services.

## Required qualification

Before C2A4 may become `EXTRACTED_AND_QUALIFIED`:

- pure codec/revision/validation tests PASS;
- real PostgreSQL 16 initialize/load/CAS-save/CAS-clear tests PASS;
- stale revision, cross-entry and tamper cases fail closed;
- non-owner runtime privileges remain exactly `SELECT, INSERT, UPDATE, DELETE` for the v119 excursion table;
- runtime TRUNCATE/ALTER/DROP remain rejected;
- runtime adapter has no migration/DDL or order capability;
- canonical-security explicitly qualifies C2A4 paths;
- full regression, import-boundary, external-roundtrip, supply-chain and release provenance remain green on one exact head and again after merge.

C2A4 completion does **not** prove Bybit credentials, Demo execution, exact-head broker evidence, strategy profitability, soak, production or live readiness.
