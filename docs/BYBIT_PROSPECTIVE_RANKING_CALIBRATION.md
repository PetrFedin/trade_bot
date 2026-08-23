# Bybit prospective ranking calibration

The live registry is refreshed every 10 minutes, but historical backtests alone cannot prove that its evidence ranking continues to discriminate future outcomes. This layer measures the ranking prospectively from the immutable v112 shadow observations created after each source signal was first available.

## Question answered

The report asks whether candidates classified and ranked **before** future bars existed subsequently behaved differently:

- `QUALIFIED_POSITIVE_EVIDENCE` versus `QUALIFIED_MIXED_EVIDENCE`;
- positive evidence versus `NO_SAMPLE_SUFFICIENT_EXACT_CELL`;
- positive evidence versus `DERIVATIVES_CONTEXT_INCOMPLETE`;
- evidence-rank buckets versus market-rank buckets;
- LONG versus SHORT;
- symbols and qualification-state × rank-bucket combinations.

It does not retune thresholds or weights and it cannot authorize a trade.

## Observation grain and duplicate protection

One prospective observation is one unique:

```text
symbol × side × signal_available_at
```

A signal may be visible in more than one 10-minute registry snapshot. Counting every repeated snapshot would duplicate the same future market path and inflate sample size. The PostgreSQL reader therefore keeps the **earliest observed seed** for each exact signal identity and the **earliest final 240-minute evaluation** for that seed.

The report exposes both `raw_final_seed_count` and `deduplicated_signal_observation_count`, plus the number removed as duplicate signal observations.

## Outcomes

Each final observation carries the already-qualified v112 prospective fields:

- first touch: `TARGET_FIRST`, `STOP_FIRST`, `AMBIGUOUS_SAME_BAR`, or `NEITHER`;
- modeled first-touch PnL only when target/stop order is known;
- MFE R and MAE R;
- 15-minute directional return and modeled net PnL;
- 60-minute directional return and modeled net PnL;
- 240-minute directional return and modeled net PnL.

`AMBIGUOUS_SAME_BAR` remains explicit and is excluded from the ordered target-versus-stop hit-rate denominator. It is never guessed into a win or loss.

## Metrics

For each group the report produces:

- observation count and fixed sample-sufficiency flag;
- target-first, stop-first, ambiguous and neither counts/rates;
- total / average / median first-touch modeled net PnL and PF where touch order is known;
- average and median MFE / MAE;
- average signal quality;
- for 15m / 60m / 240m: positive/zero/negative modeled PnL counts, positive rate, total/average/median modeled net PnL, PF, average and median directional return.

Evidence ranks use mutually exclusive buckets:

```text
1 | 2-3 | 4-5 | 6-10 | 11-20 | 21-50
```

The same buckets are reported for pure market rank so we can test whether historical evidence ranking adds discrimination beyond liquidity/market suitability rank.

## Positive evidence versus controls

For every control qualification state the report shows the prospective delta versus `QUALIFIED_POSITIVE_EVIDENCE` for:

- ordered target-first rate;
- average MFE and MAE;
- average modeled net PnL at 15m / 60m / 240m;
- positive modeled PnL rate at each horizon;
- average directional return at each horizon.

The default descriptive group minimum is 30 observations and the default comparison minimum is 50 observations **per side of the comparison**. Smaller groups remain visible but are marked insufficient.

These thresholds are reporting guardrails, not tuned trading parameters.

## Statistical interpretation

This report deliberately does **not** emit a p-value or claim statistical significance. Repeated crypto signals can remain serially correlated by symbol and regime, so naive independent-sample significance would overstate confidence. A later inference layer may add block/bootstrap or clustered uncertainty only after enough prospective history accumulates.

Therefore:

- association is not causation;
- positive calibration is not a profitability guarantee;
- one strong short window cannot promote the strategy;
- missing or insufficient control samples remain unresolved rather than being treated as success.

## Command

All available final prospective history:

```bash
python -m tools.report_bybit_prospective_ranking_calibration \
  --output artifacts/bybit-prospective-ranking-calibration.json
```

Rolling window:

```bash
python -m tools.report_bybit_prospective_ranking_calibration \
  --since-days 30 \
  --output artifacts/bybit-prospective-ranking-calibration-30d.json
```

The reader uses `BYBIT_OPPORTUNITY_DATABASE_DSN`. It is read-only and contains no order create/amend/cancel method.

## Safety boundary

Every report keeps:

- `trade_actionable=false`;
- `operator_review_required=true`;
- `parameter_retuning_performed=false`;
- `ranking_weights_changed=false`;
- `strategy_selection_allowed=false`;
- `strategy_promotion_allowed=false`;
- `demo_activation_allowed=false`;
- `live_activation_allowed=false`;
- `bybit_live_order_routing_allowed=false`;
- `causal_claim_allowed=false`;
- `statistical_significance_claim_allowed=false`;
- `predictive_guarantee_allowed=false`.
