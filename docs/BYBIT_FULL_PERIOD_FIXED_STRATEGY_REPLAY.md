# Bybit full-period fixed-strategy replay

## Purpose

v113 creates an append-only 5-minute price-history substrate. This layer turns a complete
v113 history into a fixed-strategy replay without silently falling back to the existing
28-day micro window.

The contract is deliberately narrower than the final derivatives evidence matrix:

```text
verified archive-day coverage
-> verified continuous 5m price grid
-> unchanged qualified fixed strategy
-> one full-period replay per symbol
-> price-only historical conditions
```

Full-period OI, account-ratio, funding, liquidation, and order-book claims remain blocked
until those sources have independent coverage manifests.

## Two different meanings of "full period"

A current Top-10 can contain instruments listed on different dates. Therefore two periods
must not be conflated:

1. **Per-symbol full period** — each coin from its own listing bucket through the last
   completed archive day. This PR implements that period as an independent single-symbol
   diagnostic replay.
2. **Common portfolio period** — all selected symbols compete for shared capital only
   from the latest listing/beginning that is common to the frozen universe. That is a
   different analysis and must be reported separately.

The per-symbol replay is useful for the requested `coin × LONG/SHORT × regime × condition`
history because it does not discard years from older coins merely because a newer Top-10
member listed later. It is not presented as a shared-capital portfolio backtest.

## Price-grid gate

An archive file existing for a day is not enough. Trade-archive aggregation can produce a
day with a missing 5-minute bucket if no usable trade was observed in that interval.
Running EMA/ATR logic across such a gap as though five minutes elapsed would distort time.

Before replay, `audit_full_period_5m_price_grid()` requires an exact UTC 5-minute grid:

- first expected bar = the 5-minute bucket containing the instrument launch timestamp;
- last expected bar = 23:55 UTC on the latest completed archive day;
- every expected bucket must exist exactly once;
- no out-of-range or duplicate bucket is allowed.

Any missing or extra bucket makes `full_period_price_grid_complete=false` and the replay
fails closed. Missing price intervals are not interpolated in this layer.

## Fixed strategy equivalence

The replay uses the same qualified baseline contract already used by the evidence stack:

- `CryptoPerpStrategyConfig()` unchanged;
- `CryptoProtectionPolicy()` unchanged;
- `CryptoProfitRunnerPolicy()` unchanged;
- `CryptoRunnerAdmissionPolicy(minimum_expected_edge_multiple=1.50)`;
- 5-minute interval;
- no session overlay;
- no correlation overlay;
- no execution overlay;
- no parameter search or retuning.

A regression test compares this replay directly with the existing
`CONDITIONAL_1_5X` baseline on the same synthetic acquisition. A custom strategy config is
rejected.

A deterministic strategy-contract fingerprint is emitted with every result. It fingerprints
the fixed configuration/policy contract and contract version. It is not presented as a
hash of the Python source itself; repository commit identity remains the code-version
reference.

## Memory and artifact model

`python -m tools.research_bybit_full_period_fixed_strategy` processes one selected symbol
at a time. This avoids loading years of 5-minute data for all Top-10 symbols into memory at
once.

The command:

1. resolves a complete current dynamic Top-10 on the selected regional Bybit host;
2. requires 100% v113 archive-day coverage for those symbols;
3. loads one symbol's immutable v113 bars;
4. requires a complete 5-minute grid;
5. runs the unchanged fixed strategy;
6. runs the existing historical condition diagnostics;
7. writes one JSON artifact for that symbol;
8. repeats for the next symbol;
9. atomically publishes a manifest only after all ten symbols succeed.

The output directory must be new. A failed run removes its staging directory and never
publishes a final manifest as if research were complete.

## What the result means

A successful manifest can truthfully state:

```text
price_history_full_period=true
price_grid_full_period=true
strategy_parameters_changed=false
parameter_retuning_performed=false
```

It must still state:

```text
portfolio_competition_modeled=false
derivatives_history_full_period=false
full_period_evidence_matrix_allowed=false
```

The last flag remains false because the requested final evidence matrix includes OI delta,
crowding, prior funding, stress, and execution economics. Price completeness alone cannot
make those historical variables complete.

## Safety boundary

This is research only:

```text
strategy_promotion_allowed=false
demo_activation_allowed=false
live_activation_allowed=false
bybit_live_order_routing_allowed=false
```

No create/amend/cancel order method is introduced. Historical associations are evidence,
not causal proof and not a guarantee of future profit.
