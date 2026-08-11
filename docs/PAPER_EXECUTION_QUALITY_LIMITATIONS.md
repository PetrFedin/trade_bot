# Paper Execution Quality Limitations

`PaperExecutionQualityTracker` is observational evidence, not a promotion gate.

## Historical replacement baseline

For an ordinary order, the immutable OMS limit is a stable execution baseline. For a protective order that has been replaced, the tracker can use an `EffectiveLimitProvider` such as the durable mutation store so a fill is compared with the confirmed replacement limit rather than the stale original price.

There is one remaining replay edge case that must stay explicit:

1. a partial fill occurs under replacement limit A;
2. the strategy later confirms another replacement limit B for the remaining quantity;
3. the earlier fill is first observed or reconstructed only after B is already the latest mutation state.

A provider that exposes only the **current** effective limit cannot prove whether A or B was active at the earlier fill timestamp. Recomputing that earlier fill against B would produce false slippage evidence.

Therefore:

- exact trade PnL remains authoritative because it uses the broker fill price itself;
- execution slippage for non-replaced orders is directly usable;
- execution slippage observed and persisted at the time of a replaced-order fill is useful operational evidence;
- late reconstructed replaced-order slippage must not be used for strategy promotion until the mutation layer exposes a time-indexed effective-limit lookup or the fill event persists the broker's effective order limit at fill time;
- duplicate/recovery handling must not silently rewrite an already persisted execution baseline.

## Required follow-up before promotion

The preferred durable fix is to persist the broker-effective order limit together with the exact execution event, or to expose mutation history as `limit_price_at(intent_id, occurred_at)` and use that for reconstructed fills.

Until one of those paths is implemented and tested with out-of-order partial fills plus multiple replacements, replaced-order slippage remains a diagnostic metric only.
