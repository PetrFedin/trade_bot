# Bybit prospective liquidation calibration

## Purpose

This layer measures whether the forward-only, coverage-qualified liquidation context captured by
v116/v117 adds **out-of-sample descriptive discrimination** to the fixed strategy's already-recorded
prospective v112 outcomes.

It is a report layer, not a trading rule.

## Dataset contract

The base population is the exact final v112 prospective calibration population already used by the
qualified ranking-calibration path:

- one retained observation per unique `symbol × side × signal_available_at`;
- earliest retained seed for a repeated signal;
- earliest final 240-minute outcome for that retained seed;
- final 15m / 60m / 240m prospective outcomes only.

Every base observation remains in dataset accounting even when v117 liquidation context is missing
or coverage-unqualified. This prevents the report from silently selecting only periods where the
new data feed happened to be healthy.

Context states are:

- `NOT_MATERIALIZED`;
- `COVERAGE_UNQUALIFIED`;
- `COVERAGE_QUALIFIED`.

Only `COVERAGE_QUALIFIED` rows enter liquidation-pressure performance groups. The others remain
visible in coverage counts and blocker counts.

## Liquidation groups

No threshold is fitted from PnL.

For each fixed pre-signal window (5m / 15m / 60m), the raw sign of
`LONG estimated liquidation notional - SHORT estimated liquidation notional` defines:

- `KNOWN_ZERO` — continuous coverage proved and no liquidation event occurred;
- `BALANCED_NONZERO` — nonzero events but exact LONG/SHORT estimated notionals balance;
- `LONG_LIQUIDATIONS_DOMINANT`;
- `SHORT_LIQUIDATIONS_DOMINANT`.

The same observation is also expressed relative to the prospective trade side:

- `SAME_SIDE_LIQUIDATIONS_DOMINANT`;
- `OPPOSITE_SIDE_LIQUIDATIONS_DOMINANT`;
- `BALANCED_NONZERO`;
- `KNOWN_ZERO`.

This grouping is descriptive and predeclared. There is no learned notional threshold, percentile
cutoff or optimized liquidation score in this layer.

## Metrics

For each group, and separately by side and symbol where available, the report emits:

- sample size and sample-sufficiency status;
- ordered target-first / stop-first behavior;
- average and median MFE in R;
- average and median MAE in R;
- 15m / 60m / 240m modeled net PnL;
- win rate;
- profit factor;
- average and median directional return;
- chronological cumulative outcome-sequence drawdown.

The drawdown field is deliberately labelled as an **outcome-sequence diagnostic**. It is not a
shared-capital or mark-to-market portfolio drawdown; the qualified shared-capital replay remains the
proper source for portfolio competition and concurrency behavior.

## Sample gates

The fixed descriptive gates match the existing prospective ranking-calibration philosophy:

- 30 observations for a group to be labelled sample-sufficient;
- 50 observations on each side of a pairwise comparison.

A small favorable sample is reported as insufficient. The diagnostic does not emit naive p-values
because repeated crypto signals are serially dependent and the current layer does not claim an
independent-identically-distributed sample.

## PostgreSQL reader

`PostgresCryptoProspectiveLiquidationCalibrationReader` is read-only. It:

1. loads the qualified final prospective base dataset through the existing v112 reader;
2. loads v117 context for exactly those retained seed IDs;
3. verifies symbol, side and signal-time identity against the base observation;
4. retains observations with missing/unqualified context in dataset accounting;
5. requires complete typed 5m/15m/60m rows before calling context coverage-qualified.

It has no broker client, no mutation method and no order-routing capability.

## Operational report

```bash
export BYBIT_OPPORTUNITY_DATABASE_DSN='postgresql://...'
python -m tools.report_bybit_prospective_liquidation_calibration \
  --output artifacts/bybit_prospective_liquidation_calibration.json
```

A bounded recent report can use `--rolling-days N`. The default is all available **forward
liquidation** history. This wording is important: it is not full instrument history, because v116 is
forward-only from the date capture began.

## Safety boundary

Every report states:

- `liquidation_feature_used_for_source_ranking=false`;
- `parameter_retuning_performed=false`;
- `ranking_weights_changed=false`;
- `strategy_selection_allowed=false`;
- `strategy_promotion_allowed=false`;
- `trade_actionable=false`;
- `operator_review_required=true`;
- `demo_activation_allowed=false`;
- `live_activation_allowed=false`;
- `bybit_live_order_routing_allowed=false`;
- `causal_claim_allowed=false`;
- `statistical_significance_claim_allowed=false`;
- `predictive_guarantee_allowed=false`.

## What would justify a later ranking experiment

A future ranking experiment is not justified merely because one pressure group has higher PnL in a
small sample. At minimum, the report needs stable coverage, sample-sufficient groups, consistent
read-through across several future horizons, acceptable MFE/MAE and drawdown behavior, and a
predeclared prospective test of any proposed ranking change.

Any such ranking change must be a separate PR with frozen parameters and its own prospective
qualification. This layer cannot promote itself into execution policy.
