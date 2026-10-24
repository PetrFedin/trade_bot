# ASTRA — frozen BTC prior-close entry gates, 24 October 2026

## Why these two additional experiments exist

Historical 21-coin shared-cash results are broadly negative. Earlier ex-post
BTC market-state attribution showed that both rising and falling BTC regimes
contain losses, so the existence of a regime label is NOT an alpha claim.
The next test is a separate, predeclared hypothetical execution policy:
when BTC conditions are poor, defer NEW entries without disturbing the
original signals, existing positions or original common capital model.

The original study MUST recompile to SHA:
`e2f8c37d5c6df948dccfbb054ad2941a443ba2b5b3015bdfadfee44ee82f9613`.
Existing Strategy A, OMS, main and live/demo permissions are untouched.

## Two frozen gate variants

- `BTC_SMA50_BLOCK`: block each NEW candidate entry at UTC day T open if
  the already completed BTC day T-1 close is below the previous 50-day BTC
  SMA. Previous day closes only; no current open, high or close leakage.
- `BTC_VOLATILITY_100_BLOCK`: block each NEW entry if population standard
  deviation of the prior 20 BTC close-to-close UTC daily returns,
  annualized using sqrt(365), exceeds 100% per year. Engineering stress
  threshold selected a priori, not fitted against historical outcomes.

In both policies the rejection reason is `BTC_PRIOR_CLOSE_ENTRY_GATE`,
and the ungated ranked signals are retained so opportunity cost is visible.
Existing positions still follow original 3% stop, 6% target and 8-day
time exit; a gate does NOT liquidate positions or imply a continuous
risk-neutral portfolio. Same-day exit proceeds cannot fund earlier entries.

## Acceptance matrix

Four original research arms × two original entry ranking policies ×
two entry-gate types × two original cost assumptions (8/5 and 10/10 bps
on each side) × two synchronized universes (6 and 21 coins) =
**64 full historical common-capital scenario candidates**.

Benchmark is the same frozen, ungated two-position BTC+ETH portfolio in
the 36-model original; no adapted control benchmark is introduced.
All models use one $10k cash ledger, 29% target size, two positions,
60% opening exposure limit, and conservative daily stop-first assumptions.

The experimental report retains each trade, model cash equity path and
decision trace. Per-model delta is measured against exactly the same
unchanged original universe/arm/ranker/cost tuple. It is NOT a signal to
automatically promote the gate if its historical return is higher.

## Financial provenance and safety

- Pin 21 raw source SHA-256 checksums and the matched 981-day original
  2023-10-09 through 2026-06-15 time spine.
- Require exact original historical portfolio digest before running variants.
- Prefix-invariance tests: changing or truncating data after an entry
  cannot change earlier decisions, gate, daily equity or closed trades.
- Reject nonfinite execution costs, invalid or unknown gate names and
  benchmark retroactive modification.
- Independently reconcile each closed trade gross - hypothetical fees -
  slippage = model net and all 64 terminal cash accounts.
- No genuine historical order-book depth, Mark Price funding settlement,
  account fee tier, margin/liquidation, queue, partial fills, or
  untouched future market test is available.

Everything is RESEARCH ONLY, no demo/live order authority.
`PROFITABILITY_NOT_PROVEN`.
