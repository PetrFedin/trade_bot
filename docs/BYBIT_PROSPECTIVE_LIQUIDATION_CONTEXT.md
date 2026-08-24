# Bybit prospective liquidation context v117

## Purpose

v117 joins the forward-only v116 public liquidation capture to the existing v112 prospective
shadow observations without changing the strategy, the historical evidence rank or any execution
policy.

The unit of analysis is one immutable v112 `seed_id`. For that seed, v117 reconstructs only the
liquidation information that was observable **before** `signal_available_at`.

## Anti-lookahead boundary

For every seed:

```text
coverage_start = signal_available_at - 60 minutes
feature event interval = [coverage_start, signal_available_at)
```

An event whose exchange event timestamp is equal to or later than `signal_available_at` is rejected
from the pre-signal context. Later liquidation events can be studied separately as outcome
attribution, but they are never pre-entry features.

v117 creates fixed 5m, 15m and 60m windows ending exactly at signal availability.

## Coverage is evidence, not an assumption

A missing liquidation row is not automatically zero.

Before v117 can expose numerical liquidation features it requires one v116 subscription containing
the symbol to cover the complete 60-minute lookback. The coverage proof requires:

- the subscription existed before the 60-minute window began;
- the latest status at the window start was `CONNECTED` or `HEARTBEAT` and fresh;
- there is no `DISCONNECTED` or `STOPPED` inside the window;
- there is no other non-connected stream state inside the window;
- the gap between consecutive connected/heartbeat observations never exceeds the fixed status-age
  bound;
- the final status at or before `signal_available_at` is connected/heartbeat and fresh.

If any condition fails, `coverage_qualified=false` and **all 5m/15m/60m feature metrics are NULL**.
They are not zero-filled.

Only when the full coverage proof passes can a window with no stored liquidation events become a
`known_zero=true` row with typed zero metrics.

## Stored metrics

For each coverage-qualified 5m / 15m / 60m pre-signal window:

- event count;
- LONG-liquidation count;
- SHORT-liquidation count;
- LONG estimated liquidation notional at bankruptcy price;
- SHORT estimated liquidation notional at bankruptcy price;
- total estimated liquidation notional;
- LONG minus SHORT estimated liquidation notional;
- normalized signed imbalance in `[-1, +1]`;
- largest event estimated liquidation notional;
- first and last liquidation event timestamps.

The v116 methodology still applies: `quantity × bankruptcy price` is an estimated liquidation
notional, not the liquidated trader's realized loss or an exchange-accounting amount.

## Persistence

Migration `migrations/v117/001_bybit_prospective_liquidation_context.sql` adds:

- `astra_bybit_shadow_liquidation_context_v117` — one immutable context header per v112 seed;
- `astra_bybit_shadow_liquidation_window_v117` — exactly the application-defined 5/15/60-minute
  context rows.

The header links to the original v112 seed and, when coverage is qualified, to the exact v116
subscription used to prove observation coverage. UPDATE and DELETE are rejected by database
triggers and PUBLIC privileges are revoked.

## Operational materialization

The materializer is PostgreSQL-only:

```bash
export BYBIT_OPPORTUNITY_DATABASE_DSN='postgresql://...'
python -m tools.materialize_bybit_liquidation_research_context \
  --limit 100 \
  --minimum-signal-age-seconds 120 \
  --maximum-status-age-seconds 60 \
  --migrate-postgres
```

It processes v112 seeds that are old enough for the coverage evidence to have arrived and do not yet
have a v117 context. It never calls Bybit and has no order client.

## Safety and interpretation boundary

Every context remains:

- `prospective=true`;
- `liquidation_feature_used_for_source_ranking=false`;
- `parameter_retuning_performed=false`;
- `trade_actionable=false`;
- `strategy_promotion_allowed=false`;
- `demo_activation_allowed=false`;
- `live_activation_allowed=false`;
- `bybit_live_order_routing_allowed=false`.

A v117 row is descriptive evidence. It does not prove causality and does not authorize a strategy
change.

## Next bounded step

After enough **coverage-qualified** v117 contexts also have final v112 outcomes, build a descriptive
out-of-sample calibration report that compares future PnL / PF / win rate / MFE / MAE across
predeclared liquidation-context groups while preserving:

- the original historical evidence rank;
- the original market rank;
- the original fixed strategy parameters;
- the original source qualification state.

Only if that prospective comparison becomes sample-sufficient should a later, separate change even
consider whether liquidation context belongs in ranking. No ranking-weight change is part of v117.
