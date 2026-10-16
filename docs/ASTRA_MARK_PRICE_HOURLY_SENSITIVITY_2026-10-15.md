# ASTRA — six-major hourly Mark Price funding sensitivity

Date: 2026-10-15. **Research-only coarse source experiment.**
Dependency: Draft PR #325 defines the final settlement-aligned **1-minute**
Mark Price admission contract. This branch deliberately does NOT weaken that
contract.

## Question

The exact-head PR #324 cash-feedback replay materially improved the six-major
historical result after source-verified hourly stop/target ordering. Its
funding cashflow still uses a research notional convention rather than
historical venue Mark Price. Before paying the cost of collecting and
reviewing every 1-minute settlement candle, measure how wide the Mark Price
uncertainty is at the coarser 60-minute level.

This is a **source sensitivity test**, not a parameter search. No entry, exit,
stop, target, ranking, universe, fee, slippage or funding-rate assumption is
optimized from its result.

## Fixed source and chronology

Universe:
BNBUSDT, BTCUSDT, DOGEUSDT, ETHUSDT, SOLUSDT, XRPUSDT.

Window:
2023-10-09T00:00:00Z through 2026-06-14T23:00:00Z.

The end date matches the common fully retained funding coverage used by the
six-major vs broad-21 PR #324 comparison. We do not extend to a date for which
the broad-21 retained funding source is incomplete.

Source:
Bybit public V5 **Mark Price Kline**, category linear, interval 60 minutes,
maximum page size 1000. The collector pages deterministically over every UTC
hour. For each API page it preserves the query and SHA-256 of a canonicalized
provider result payload. The raw hourly CSV is also SHA-256 pinned.

No Last Price Kline, entry price, daily close, interpolation, nearest-hour
fill or fabricated candle may satisfy this experiment.

## Funding intersection

For each symbol, the retained funding CSV and its
bybit-funding-snapshot-v2 manifest are re-hashed before use. Every funding
settlement inside the window must map to the Mark Price hour beginning at the
same UTC timestamp.

The output stores for each settlement:
- funding rate;
- Mark Price hourly open/high/low/close;
- hourly Mark range as fraction of the high/low midpoint.

The six-symbol summary independently re-hashes raw and settlement CSVs and
requires identical collection start/end across all symbols.

## Interpretation

The hourly high/low range is deliberately conservative and coarse. It answers:
"How wide could Mark Price have moved during the settlement hour?" It does NOT
answer the exact Mark Price applied by Bybit at the settlement instant.

Fields therefore remain false:
- exact settlement mark verified;
- compatible with final 1m admission;
- account funding ledger verified;
- strategy replay completed;
- strategy promotion allowed;
- profitability proven.

If hourly ranges are already large enough to affect the #324 economic
conclusion, the 1-minute collection becomes P0 immediately. If they are small,
the result only reduces uncertainty; it does not remove the requirement for
the final 1-minute source/admission before a stronger profitability claim.

## Next after source collection

1. Preserve all six raw hourly sources, settlement intersections and page
   digests as a GitHub Actions artifact.
2. Feed low/high Mark Price envelopes into the exact #324 trade quantities and
   funding participation logic without changing Strategy A.
3. Measure how much the six-major +12.82195% matched-window UPPER_COST result
   can move solely from Mark Price notional uncertainty.
4. Then collect settlement-aligned 1-minute Mark Price evidence under PR #325
   and repeat.
5. Independent reviewer checks arithmetic, source provenance and all adverse
   outcomes.

No paper/live/broker permission. PROFITABILITY_NOT_PROVEN.

## 2026-10-16 — Page-level reproducibility hardening

The hourly source collector now retains canonical decoded JSON provider responses
for each requested page under `provider_pages/`, with hashes for both the
complete response representation and the nested provider result. These files
are **not** the original HTTP wire bytes and contain no exchange signature.
The independent archive verifier requires every retained response page to match
the exact fixed symbol, linear category, 60-minute query range, timestamp set,
and raw hourly Mark Price values. It also verifies original funding rates and
settlement intersections; only then can the six-major summary be produced.

New negative fixtures intentionally modify provider candles and re-hash both
response and result: these must still fail against independent CSV evidence.
Missing response pages fail closed. The CI collector still fails on Bybit HTTP
403 with a blocked-source artifact: no substitute candles, no profitability
promotion, no access-control circumvention, no modification of Strategy A.

Old hourly source archives without page bodies cannot satisfy this new evidence
contract; re-collect legitimate source data or mark the study incomplete.

## 2026-10-16 — Deterministic offline source replay

`python -m tools.replay_mark_price_hourly_archive` can now reconstruct a
historical Mark Price archive **from previously captured provider responses**,
with zero HTTP calls. The operator must pass: `--archived-root`,
`--funding-root`, `--output-root`, `--symbol`, `--start-utc`,
`--end-utc`, and `--expected-manifest-sha256` (a detached, externally
recorded SHA-256 of the original per-symbol Mark Price manifest).
The existing archive undergoes cross-source verification first. Every saved
response page is then replayed through the normal collector's validation
and page-continuity logic. New CSV and manifest bytes must reproduce the
detached manifest hash exactly; the destination is published only after
verification, without overwrite. Tampered/missing pages and changed windows
fail closed. No live/broker authority is added.

A detached hash establishes consistency with the **approved archive pin**,
not Bybit authorship: independent provider-origin attestation is still absent.
Offline mode cannot create data that was never successfully acquired. HTTP 403
remains a blocked collection state. The research summary cannot qualify until
all six genuine sources pass the full admission gates.

## 2026-10-16 — All-six atomic research-source admission

A new research-only importer `tools/replay_six_major_mark_price_archive.py`
accepts an independently approved, detached-SHA-pinned JSON source-set manifest
(`astra-six-major-hourly-mark-source-set-v1`). The manifest must contain
the fixed BNB/BTC/DOGE/ETH/SOL/XRP symbol list and an individual historical
Mark Price manifest SHA-256 for every symbol, plus identical UTC start/end,
a research-only classification and explicit false trading/profitability flags.

The import first validates **all six** source files, funding archives and
provider pages against their approved pins. It then reproduces all six sources
inside one temporary directory, requires an independently audited six-symbol
summary, writes one cohort receipt, and publishes the entire output directory
using a same-filesystem rename. If any symbol fails, nothing is published;
an existing output directory is never overwritten. The receipt is a source
integrity record, not exchange provenance attestation or strategy PnL.

Example invocation (only after real archived sources and detached pins exist):

```sh
python -m tools.replay_six_major_mark_price_archive \
  --source-set /approved/six-major-source-set.json \
  --source-set-sha256 <externally-approved-64-character-hash> \
  --archived-root /archive/mark-price-hourly \
  --funding-root research/snapshots/bybit_funding_2023-10-09_2026-10-08 \
  --output-dir /research/qualified-six-major-mark
```

A signed or independently retained source-set approval is a separate
operational obligation. Hashes copied from untrusted data do not create
trusted provenance. Nothing here bypasses exchange regional/API restrictions,
adds synthetic data or grants paper/live execution permission. Full historical
collection remains blocked by the current public API HTTP 403.
