# ASTRA: post-freeze future evidence admission — 2026-10-14

**RESEARCH ONLY / NO BROKER / NO STRATEGY PROMOTION**.

Historical nested per-asset ATR experiment in [PR #319]
(https://github.com/PetrFedin/trade_bot/pull/319) failed to
consistently improve Strategy A: only 1/7 outer folds better,
3 equal and 3 worse; mean conservative funding-lower advantage
**-0.24919 percentage points per reset fold**. The whole historical
2023–2026 range was already inspected and does NOT constitute a
new, previously unknown final holdout.

Consequently, do not optimize the same outer validation failures
and call the new choice profitable. This module starts a distinct
**chronology/source gate** for any future candidate, but it does
not freeze or approve a trading candidate itself.

## What a future evaluation must declare BEFORE viewing new data

A candidate freeze manifest in JSON must be committed and
externally timestamp-witnessed before the purported final
test interval. Schema `astra-research-candidate-freeze-v1`:

- Nonempty candidate_id, 40-digit candidate Git SHA, independent
  64-digit strategy-config and selection-rules SHA-256.
- UTC `frozen_at_utc` and the end of previously inspected history;
  latter must not be later than freeze.
- Exact six major symbols canonicalized; minimum 90 future
  **fully closed** daily bars per symbol.
- Explicit `research_only=true` and
  `strategy_promotion_allowed=false`.

This module validates the syntax but **cannot verify** the actual
Git provider timestamp, freeze authorship, or whether an individual
has secretly inspected the later data. External witness evidence
must therefore be retained separately. Do not infer that a JSON
date is a trustworthy notarized freeze.

## New source files and source-origin limits

Require complete future **daily** OHLCV for BNB/BTC/DOGE/ETH/SOL/XRP,
one CSV per symbol, exactly columns
`timestamp,symbol,open,high,low,close,volume`, monotonic
UTC-midnight daily candles, **no missing dates**, no duplicates
or partially closed days, full consistent cross-symbol timeline.
No future candle may have started on/before the candidate freeze
or the last previously inspected timestamp. At least 90 complete
candles per symbol before its evaluation can be admitted.

A separate `provenance.json` declares data_origin, source
endpoint, collector and post-freeze collection time, interval
1440 minutes, exact six-symbol universe, and SHA256 of all six
raw files. Unsupported provider claims and changed/corrupt
file digests are rejected. `TEST_FIXTURE_ONLY` is permitted
for negative CI but never for genuine research source eligibility.

The code validates **self-declared** provenance and raw
checksums. These CANNOT establish that source data truly came
from Bybit, that quotes were available at the represented time,
or that an independent custodian witnessed the freeze.
Passing returns only
`TEMPORAL_SOURCE_COVERAGE_ONLY`, with ALL of:

`provider_authenticity_independently_verified=false`,
`freeze_time_independently_witnessed=false`,
`untouched_holdout_independently_verified=false`,
`strategy_replay_completed=false`,
`strategy_promotion_allowed=false`,
`profitability_proven=false`.

A later independent source/replay qualification must do more:
reconcile true venue-provider raw source and identifiers, verify
independently recorded freeze time and immutable Git strategy
SHA, exact account costs and funding, intraday stop/target
touch order, no order-book assumptions presented as executions,
and compare a frozen candidate on all newly received days
against unchanged Strategy A, cash, BTC and capital-matched
benchmarks, with all losses, turnover and drawdowns included.

## Negative CI

The workflow tests source datasets with future dates but
**synthetic** values solely as test fixtures. It rejects
old history reused after freeze, too-short datasets, a missing
day, mismatched symbol/date, expired or still-open daily
candles, mismatched SHA256 and manifests with trading
authorization. It also explicitly checks that the existing
2023–2026 GitHub archive cannot be certified as a post-freeze
future source. This is negative evidence, NOT a successfully
completed new 90-day trading strategy test.

## Release disposition

No strategy is frozen for promotion by this code. No real future
data after 2026-10-14 have been supplied here. main/production,
trade execution, exchange permission and paid infrastructure
remain unchanged. `PROFITABILITY_NOT_PROVEN`.
