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
