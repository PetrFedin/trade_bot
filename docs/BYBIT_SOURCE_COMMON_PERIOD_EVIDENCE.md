# Bybit source-common-period fixed-strategy evidence

## Purpose

This layer is the bridge between the qualified historical archives and the continuously refreshed live opportunity registry.

It combines:

- v113 complete 5-minute price history;
- v114 source-aware open-interest history;
- v114 source-aware long/short account-ratio history;
- v114 funding history;
- the unchanged qualified fixed crypto strategy;
- the existing execution-economics model;
- the existing `BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX` contract consumed by the live evidence registry.

The output is not a new score and does not retune the strategy. It replaces a shorter evidence window with the longest period that all required historical inputs can honestly support for each current Top-10 symbol.

## The central coverage rule

For each symbol:

```text
common_start = max(
    price source start,
    open-interest source start,
    account-ratio source start,
    funding source start
)
```

The end is the first UTC instant after the latest completed archive day shared by the qualified v113/v114 coverage plans.

For symbols listed before July 20, 2020, the public Bybit account-ratio source itself starts later than the instrument. Therefore a combined price + OI + crowding + funding matrix beginning in 2020 is the **maximum source-available common period**, not the instrument's full lifetime.

The implementation preserves both statements separately:

- `source_available_common_period_matrix=true` means every required source is complete over the period it can legitimately provide;
- `instrument_lifetime_combined_matrix_claim_allowed=true` only when all required sources genuinely cover the instrument from listing.

No missing pre-source history is filled, interpolated, inferred or relabeled as observed history.

## Per-symbol replay contract

Each selected Top-10 symbol is replayed independently from its own common start through the common archive cutoff.

The replay uses the same qualified fixed contract as the full-period price replay:

- default `CryptoPerpStrategyConfig`;
- default `CryptoProtectionPolicy`;
- default `CryptoProfitRunnerPolicy`;
- `CryptoRunnerAdmissionPolicy(minimum_expected_edge_multiple=1.50)`;
- 5-minute completed bars;
- no parameter search, optimization or symbol-specific retuning.

The independent replay intentionally does **not** model competition for one shared account balance across the Top-10. Each symbol uses the configured reference equity only for comparable strategy diagnostics. The artifact therefore states:

```text
portfolio_competition_modeled=false
```

A later common-portfolio replay must be a separate experiment on the intersection of symbol timelines.

## Point-in-time derivatives contract

For every closed strategy trade, derivatives features are joined only from observations known at or before `decision_time`:

- current and previous OI → OI delta / regime;
- latest account-ratio → crowding regime;
- latest prior funding → funding regime.

Funding events strictly after entry and at or before exit remain post-entry attribution and are not allowed to become pre-entry features.

This reuses the already-qualified point-in-time join logic and therefore preserves the no-lookahead contract.

## Evidence dimensions and outputs

All per-symbol rows are combined through the existing matrix builder. The grain remains:

```text
symbol
x LONG/SHORT
x market regime
x OI regime
x crowding regime
x prior-funding regime
x stress regime
```

The matrix preserves the existing evidence statistics:

- trade count and sample sufficiency;
- win rate;
- total and average net PnL;
- profit factor;
- average MFE / MAE;
- chronological trade-sequence drawdown;
- average turnover;
- expected edge;
- modeled round-trip execution cost;
- cost-to-edge and edge-to-risk ratios.

Historical liquidation events remain explicitly unavailable unless they are separately collected and qualified. Stress proxies must not be described as reconstructed liquidation history.

## Live-registry handoff

The resulting report keeps the existing top-level diagnostic:

```text
BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX
```

When `--persist-evidence` is used, the report is written through the existing append-only v111 evidence store. This means the already-qualified 10-minute live process can consume it without a second ranking implementation:

```text
v113 full price archive
+ v114 source-aware derivatives archive
-> fixed-strategy source-common-period evidence matrix
-> immutable v111 evidence snapshot
-> current Top-10/Top-50 + completed 5m signal
-> point-in-time current derivatives
-> exact historical evidence cell
-> evidence-ranked operator review queue
```

## Interpretation

A positive historical cell means that the fixed strategy had better observed outcomes in that historical state under the modeled execution assumptions. It does not establish why the market moved, does not prove causality and does not guarantee that the pattern will continue.

Sample size, coverage completeness, execution economics and prospective shadow outcomes remain mandatory context when evaluating a candidate.

## Safety boundary

The research output remains non-trading:

- `trade_actionable=false`;
- `operator_review_required=true`;
- `strategy_parameters_changed=false`;
- `parameter_retuning_performed=false`;
- `strategy_selection_allowed=false`;
- `strategy_promotion_allowed=false`;
- `demo_activation_allowed=false`;
- `live_activation_allowed=false`;
- `bybit_live_order_routing_allowed=false`.

The real Bybit mainnet account remains a separate authenticated read-only boundary. This evidence layer has no create/amend/cancel order surface.
