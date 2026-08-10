# AAPL third-party multi-regime sample

This directory contains a **non-authoritative historical sample** used to test the deterministic strategy-qualification boundary with observed market-price data instead of generated prices.

## Provenance

- upstream repository: `plotly/datasets`
- upstream path: `finance-charts-apple.csv`
- upstream Git blob: `7b1bab3953bb5cdf47e84de1048ca04b0c991987`
- repository license reported by the upstream repository: MIT
- upstream market-data origin: not asserted here as exchange-authoritative
- source classification enforced by code: `THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE`

The snapshot contains three selected 20-session windows from the upstream AAPL sample. `Date` is represented as a UTC observation date at `00:00:00Z`, `AAPL.Close` is mapped to `close`, and the symbol is fixed to `AAPL`. The file and canonical bar stream are SHA-256 pinned in `manifest.json`.

## Current qualification policy (v1)

`qualification.json` is the current acceptance policy. Thresholds were declared before CI execution so qualification does not silently tune itself to the observed outcome. The policy includes $0.50 per-fill fees, 5 bps slippage, two walk-forward windows per regime, a 10% maximum drawdown ceiling and -3% floors for mean OOS and excess return.

The benchmark semantics are versioned. `capital_matched_buy_hold_v1` compares strategy portfolio return against a buy-and-hold control using the **same target quantity, opening cash, entry slippage and per-fill fee**. The raw AAPL price return is retained only as an informational asset benchmark, while a zero-return cash baseline is recorded separately.

The v1 activity floor is explicitly `minimum_active_windows=0`. This preserves the policy that produced the current canonical qualification evidence; it is not silently tightened after observing that the drawdown regime contained zero trades.

## Next-cycle shadow policy (v2)

`qualification_next_cycle.json` is deliberately **not** the current acceptance policy. It is a predeclared shadow policy for the next data/strategy qualification cycle. It keeps the same costs, return floors, drawdown ceiling and benchmark semantics but requires at least one active OOS window per regime with `minimum_active_windows=1`.

Against the current 60-row sample this v2 policy is expected to fail only `drawdown_2016_spring` with `INSUFFICIENT_ACTIVE_WINDOWS`. CI asserts and archives that negative result. The purpose is to make the next blocker executable before a broader dataset is introduced, rather than changing current acceptance retrospectively.

A future dataset/policy version should only promote the activity requirement into the primary acceptance path after the broader historical sample is selected and versioned independently of the observed qualification outcome.

This sample improves reproducibility and exercises real observed price paths. It does **not** replace an authoritative licensed market-data feed, broader multi-asset/multi-cycle coverage, or a production-calibrated strategy validation program.
