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

## Qualification policy

`qualification.json` is versioned separately from the data. Thresholds are declared before CI execution so qualification does not silently tune itself to the observed outcome. The current policy includes $0.50 per-fill fees, 5 bps slippage, two walk-forward windows per regime, a 10% maximum drawdown ceiling and -3% floors for mean OOS and excess return.

This sample improves reproducibility and exercises real observed price paths. It does **not** replace an authoritative licensed market-data feed, a broader multi-asset benchmark, or a production-calibrated strategy validation program.
