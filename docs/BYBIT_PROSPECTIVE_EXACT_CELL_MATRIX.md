# Bybit prospective exact evidence-cell matrix

## Purpose

This layer turns the existing historical evidence-cell architecture into a forward validation matrix.
For every retained final v112 prospective observation it reloads the **exact v111 candidate that
existed at signal time** and keeps the source conditions separate from the future outcome.

The target grain is:

```text
symbol
× LONG / SHORT
× market regime
× open-interest regime
× crowding regime
× prior funding regime
× stress regime / score
× exact evidence_cell_key
→ prospective PnL / PF / win rate / MFE / MAE / sequence drawdown / sample size
```

A second optional cut adds the already-qualified 15-minute pre-signal liquidation profile from
v117, but liquidation never changes the source rank in this report.

## Source-time identity

For each retained v112 seed the PostgreSQL reader joins the original v111 candidate by
`source_snapshot_id + symbol` and independently verifies:

- symbol;
- LONG/SHORT side;
- evidence rank;
- market rank;
- qualification state;
- decision timestamp;
- signal quality.

A mismatch fails instead of silently joining a later candidate.

The exact-cell context is considered complete only when the source candidate has:

- `evidence_cell_key`;
- market regime;
- OI regime;
- crowding regime;
- prior-funding regime;
- stress regime;
- stress score;
- historical evidence trade count.

Observations whose source exact cell was incomplete remain in dataset accounting as
`CELL_UNAVAILABLE`; they are not zero-filled into a fabricated regime.

## Historical reference versus prospective outcome

The report deliberately carries two different evidence blocks.

### Source historical reference

These values are the exact historical-cell values visible in the source v111 candidate at the time
of the signal:

- trade count and sample-sufficiency flag;
- profit factor;
- win rate;
- total and average historical net PnL;
- average MFE / MAE;
- historical sequence drawdown;
- positive-historical-evidence flag.

When the same semantic cell is observed across multiple later source snapshots, the report shows
min / median / max of source-time historical values rather than pretending they were one immutable
number across time.

### Prospective outcome

The future side uses only final v112 prospective outcomes:

- 15m / 60m / 240m modeled net PnL;
- profit factor;
- win rate;
- average and median directional return;
- average and median MFE / MAE;
- chronological prospective outcome-sequence drawdown;
- sample size and fixed sample-sufficiency flag.

The prospective drawdown is an observation-sequence diagnostic, not shared-capital portfolio
mark-to-market drawdown. Shared-capital competition remains the responsibility of the already
qualified common-period portfolio replay.

## Report surfaces

The report exposes:

1. the full exact-cell matrix;
2. `symbol × side` matrix;
3. separate tables by market / OI / crowding / prior funding / stress regime;
4. an exact-cell + 15m liquidation-profile matrix for rows whose v117 context is
   `COVERAGE_QUALIFIED`.

The liquidation-augmented matrix does not hide rows where liquidation context is unavailable; those
rows remain counted in the parent dataset and are simply ineligible for that additional cut.

## Sample gate

The default exact-cell sample-sufficiency gate is 30 prospective observations. A cell below the gate
is still reported, but it is explicitly insufficient and cannot become an automatic trading rule.

No fitted thresholds, automatic regime pruning or post-hoc LONG/SHORT disabling are introduced.

## Operational report

```bash
export BYBIT_OPPORTUNITY_DATABASE_DSN='postgresql://...'
python -m tools.report_bybit_prospective_exact_cell_matrix \
  --output artifacts/bybit_prospective_exact_cell_matrix.json
```

Use `--rolling-days N` for a bounded recent view. The default uses all retained final prospective
observations available in PostgreSQL.

## Safety boundary

Every report keeps:

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

The purpose is to identify where historical cell evidence is or is not confirmed prospectively.
Any later change to ranking or strategy policy must be a separate, predeclared prospective
experiment.
