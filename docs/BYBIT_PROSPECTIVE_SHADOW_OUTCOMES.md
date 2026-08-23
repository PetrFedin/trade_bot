# Bybit prospective shadow outcomes

## Purpose

The live evidence registry ranks current fixed-strategy opportunities from retrospective
historical evidence. This layer adds a separate prospective, out-of-sample audit trail.
It does not place a trade. It records a candidate before its future is known and then
measures what happened as completed 5-minute bars arrive.

The intended chain is:

```text
current Top-10 / Top-50 market registry
-> fixed LONG/SHORT signal
-> OI / crowding / prior funding / stress / execution economics
-> exact historical evidence cell
-> ranked live opportunity snapshot v111
-> immutable prospective shadow seed v112
-> completed future 5m bars only
-> 15m / 60m / 240m outcome + target/stop sequence + MFE/MAE
-> append-only prospective evidence
```

## Why this matters

A retrospective matrix can still overstate a pattern because the research period was
already observed. The v112 registry creates a timestamped forward sample that was not
available when the opportunity was ranked. Over time it can answer whether historically
positive cells continue to outperform mixed, insufficient-sample, or incomplete-context
control cohorts.

Association remains association. A strong prospective relationship is useful evidence,
not proof of causality and not a profit guarantee.

## Signal-time semantics

The fixed strategy currently stores `decision_time` as the start timestamp of the
completed 5-minute decision bar, while the bar close used by the signal is only knowable
five minutes later. The shadow layer therefore records both:

- `decision_bar_start_at` — source strategy timestamp;
- `signal_available_at` — exactly five minutes later.

No outcome bar is admitted before `signal_available_at`. A bar is eligible only after the
whole 5-minute bar is completed. This prevents the shadow evaluator from using the
signal bar itself or an unfinished future bar.

## Immutable seed reconstruction

The source v111 candidate already contains side, signal quality, planned notional, risk
budget, and modeled round-trip cost. Before v112 accepts a seed, the evaluator refetches
only the historical bars ending at the source decision bar and reruns the unchanged
qualified `CryptoPerpStrategyConfig`.

The reconstructed signal and plan must match the stored source exactly. Any difference in
side, decision timestamp, quality, notional, risk budget, or modeled cost fails closed.
The existing `execution_levels()` contract is then reused to derive the shadow entry,
stop, and target. There is no parallel target/stop formula in the shadow layer.

Trackable cohorts are:

- `QUALIFIED_POSITIVE_EVIDENCE`;
- `QUALIFIED_MIXED_EVIDENCE`;
- `NO_SAMPLE_SUFFICIENT_EXACT_CELL`;
- `DERIVATIVES_CONTEXT_INCOMPLETE`.

The last two are intentionally retained as control cohorts. Candidates with no signal or
without an accepted trade plan cannot form a shadow seed.

## Outcome contract

Every evaluation is append-only and records what was knowable at `observed_through`.
The evaluator reports:

- first target/stop touch state;
- target or stop touch timestamp when ordering is observable;
- `AMBIGUOUS_SAME_BAR` when the same 5-minute candle contains both levels;
- modeled first-touch net PnL only when ordering is known;
- directional MFE in R, non-negative;
- directional MAE in R, non-positive;
- 15-minute, 60-minute, and 240-minute mark-to-market outcomes;
- directional return, gross PnL, and modeled net PnL after the source round-trip cost;
- completed-bar count and final/non-final state.

A horizon is complete only when every expected 5-minute bar in that horizon exists.
Missing bars do not get interpolated. A 240-minute complete observation is final.

When both target and stop fall inside one 5-minute bar, the intrabar order is unknowable
from kline data. The system never chooses the favorable or unfavorable order and never
fabricates a realized PnL for that bar.

## PostgreSQL v112

`migrations/v112/001_bybit_prospective_shadow_outcomes.sql` creates:

- `astra_bybit_shadow_seed_v112`;
- `astra_bybit_shadow_outcome_v112`.

Both are append-only. UPDATE and DELETE are rejected by triggers. Safety constraints keep
all records prospective and operator-review-only while enforcing:

```text
trade_actionable=false
demo_activation_allowed=false
live_activation_allowed=false
bybit_live_order_routing_allowed=false
```

A source opportunity can produce only one canonical seed per symbol and v111 snapshot.
Each later `observed_through` can produce one canonical outcome evaluation. Newer
observations are inserted; earlier observations are never rewritten.

## Regional market-data consistency

The public kline client now accepts the same audited regional mainnet host allowlist used
by the universe and derivatives readers. A refresh configured for the EU site therefore
uses `api.bybit.eu` for public klines as well, instead of silently mixing an EU universe
with global `api.bybit.com` candles.

The kline client remains public GET-only, has no API-key requirement, and exposes no
create/amend/cancel order method.

## Operational cycle

`python -m tools.evaluate_bybit_shadow_outcomes` performs one bounded iteration:

1. read v111 candidates that have a fixed signal and accepted plan but no v112 seed;
2. reconstruct each seed from pre-decision bars only;
3. persist the immutable seed;
4. read all seeds without a final 240-minute evaluation;
5. fetch only completed post-signal 5-minute bars, capped at four hours;
6. append a new outcome evaluation for the current completed-bar boundary.

The workflow is scheduled every ten minutes. A missing PostgreSQL DSN, a known hosted
runner public-Bybit block, or an unapplied v112 migration produces an explicit status.
Unexpected evaluator failures remain CI failures rather than being silently converted to
success.

## Safety boundary

This layer is evidence collection, not execution. It does not create, amend, cancel, or
route orders and cannot activate demo or mainnet trading. Real Bybit mainnet order routing
remains disabled. Read-only account context and demo execution can be connected in later,
separately qualified changes without changing this boundary.
