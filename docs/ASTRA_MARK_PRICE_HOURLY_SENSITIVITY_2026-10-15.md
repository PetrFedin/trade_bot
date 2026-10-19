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

## 2026-10-16 — Internal linkage and failure-containment QA

The hourly workflow now separates an `offline-integrity-qa` job from the
dependent `public-source-sensitivity` job. The former tests collector,
original funding checks, page provenance, one-symbol and all-six offline
replay, and negative source scenarios. The latter alone requires real Bybit
API connectivity. This distinguishes deterministic code health from external
historical-data unavailability, without changing either acceptance threshold.
A RED network job is **not** an admitted historical source, regardless of
offline tests passing.

New failure-injection regression interrupts the fourth of six symbol replays
after three successful staged imports. The importer must remove the staging
directory and publish no target. A separate adversarial test changes the
source-set manifest to claim live-trading authority and recalculates its
detached SHA; the contract must still reject it.

These are synthetic regression fixtures. They do not test live broker/order
dispatch or independently attest Bybit provenance. `main`, Strategy A and
runtime trading permissions remain unchanged; `PROFITABILITY_NOT_PROVEN`.

## 2026-10-16 — Cross-PR funding-source linkage gate

`tools/verify_mark_funding_replay_linkage.py` binds the six-symbol hourly
Mark Price source-set receipt and raw funding archive to an independently
verified historical funding/cash replay from PR #324. The gate rehashes the
hourly source evidence, requires the correct six symbols, pinned source-set
digest, matching UTC chronology, identical funding CSV/manifest SHA-256s,
all six fee/slippage configurations and 36 labelled replay models. It rejects
claims of live authority, exchange settlement certainty or an untouched
holdout. A successful result is **source compatibility only**:
`funding_pnl_recomputed_with_mark_price=false`.

Example after data sources and funding replay artifacts are genuinely available:

```sh
python -m tools.verify_mark_funding_replay_linkage \
  --source-root /evidence/qualified-six-major-mark \
  --funding-root research/snapshots/bybit_funding_2023-10-09_2026-10-08 \
  --daily-report /evidence/from-pr324/daily-funding-report.json \
  --daily-verifier /evidence/from-pr324/independent-verification.json \
  --source-set-sha256 <detached-approved-64-hex-digest> \
  --output /evidence/linkage-receipt.json
```

The actual PR #324 independent arithmetic verifier must run first on its raw
price/funding snapshots. This linkage tool consumes its receipt but does not
replace independent accounting verification, nor does it alter Strategy A.
Until the external Mark Price collection and the full integration execution
are available, synthetic negative tests establish only code-path robustness.
The status remains `PROFITABILITY_NOT_PROVEN`.

## 2026-10-16 — Independent monetary reconciliation for linked replay

The cross-PR linkage now requires a second, separate arithmetic pass for
every 36 model/scenario records using
`tools/verify_funded_portfolio_money.py`. It checks:

- trade-level gross PnL, signed slippage, both execution fees and net PnL;
- funding settlement identity, rate, participation uncertainty, cash sign,
  booked day, per-day counters, credits/debits and net funding cash;
- open position quantity, execution cost, entry fee and terminal mark;
- closing cash = opening cash + closed net PnL + funding cash - open cost;
- closing equity = cash + marked open holdings;
- portfolio return, reported fill counts and daily-close drawdown;
- duplicate/missing events, non-finite values and false verified-Mark claims.

The linkage receipt records `money_models_reconciled=36` and a stable
`independent_money_audit_sha256`. These checks detect internal accounting
drift independently from source hashes. They cannot prove venue account
cashflows, actual fills, margin/liquidation or exact settlement Mark Price.

The monetary fixtures are deliberately synthetic; the PR #324
`verify_daily_funding_report.py` remains a separate independent historical
source+arithmetic gate. Final live-data qualification requires authentic
provider archives, an unchanged approved source set, a real PR #324 replay,
both independent arithmetic checks, and external source review. A passing
unit-test suite alone never establishes `PROFITABILITY_PROVEN`.

## 2026-10-16 — Exact-head historical money integration job

A separate `historical-money-integration` job now checks out the frozen
PR #324 research commit `6f6c3c42ef3b33a1fbeb2a45131b014ee15810f8`
without write credentials. It recomputes the full historical funded replay
using the retained snapshots, independently verifies funding cash and
portfolio PnL using PR #324's second implementation, and then runs
`tools/verify_funded_replay_integration.py` from the PR #326 branch.

The gate requires all six original configurations, the complete 36-model
stress grid, source report digest, exact historical-head pin, no fake
provider/venue-account proof, consistent matched-window dates, and a
second pass of independent net trade PnL, entry/exit fees, funding cash,
open holdings, ending cash/equity and daily-close drawdown. Any discrepancy
fails the dedicated job. The evidence receipt retains hashes and explicit
`profitability_proven=false`, `live_trading_allowed=false`.

The Mark Price collector remains a separate, currently source-blocked
job. A passing historical integration job must never turn the external
Mark Price failure GREEN or imply actual exchange execution.

## 2026-10-17 — Daily ledger continuity and strategy-decision linkage

The independent historical-money auditor now requires:
- contiguous UTC daily equity snapshots and opening/previous close carry-forward;
- explicit first execution day and final source day matching the frozen replay;
- selected instruments within the frozen universe (maximum two positions);
- every entered instrument a member of that day's modelled selection;
- the funding-book day sequence matching daily valuation dates for funding scenarios;
- no funding booked before the modelled entry, and all trade/position dates covered;
- final daily equity matching independently reconstructed cash plus open holdings.

Negative fixtures reject balance carry drift, missing funding dates, and
selection/entry mismatch. These checks evaluate model-state consistency,
not truth of exchange fills, live venue mark prices or real account margin.
External Mark Price remains BLOCKED_HTTP_403 and profitability unproven.

## 2026-10-18 — Event-sourced position lifecycle and trading-turnover audit

Independent `tools/verify_funded_position_lifecycle.py` reconstructs modelled
open/closed instrument intervals from retained trades and terminal holdings.
It must match every execution-day `entered_symbols`, reject overlaps,
same-day open exits, impossible holding duration, mixed closed/open schemas,
and more than two simultaneous end-of-day positions. For each retained
funding event, it independently binds symbol, recorded entry timestamp,
quantity, entry execution price, state (`OPEN_EXIT`, `INTRABAR_EXIT`,
`OPEN_AT_CLOSE`), and boundary uncertainty to its source position. A missing
exchange settlement cannot be reconstructed from the retained event list:
provider completeness is explicitly **not** attested.

The independent monetary verifier invokes this event audit for all 36
portfolio replay models. It additionally recalculates execution turnover
as the sum of the buy and sell notionals for all closed trades plus entry
notionals for final open holdings, divided by the original 10,000 USDT
research capital. It separately counts ambiguously classified intrabar
exits. Both are cross-checked against the reported metrics, with new
negative mutation tests.

The historical integration run preceding these final turnover additions,
`39159177487`, successfully passed 36/36 model reconciliations under
the event-sourced lifecycle gate with evidence SHA-256
`323c667c7a0110f89d8453b7746b186106e3f51cad3da8eacbb680713f4e107f`.
This is **not** the result of the later turnover changes, which require
their own exact-head CI. The live Mark Price collector separately remains
`BLOCKED_HTTP_403`. No live execution or strategy qualification permitted.

## 2026-10-19 — Restart-safe independent 36-model audit

`tools/recover_funded_money_audit.py` checkpoints each completed research
money-audit model using atomic same-directory replacement. Every checkpoint
binds the historical report and independent verifier bytes, the fixed PR #324
head, and SHA-256s of all four auditing implementation files. The checkpoint
content and individual model audit results carry SHA-256 fingerprints. Model
keys are deterministic and unique; a partial or mutated checkpoint is rejected
rather than admitted. An interrupted model is recomputed from scratch.

When the resumed 36-model pass finishes, an **uncached** complete 36-model
audit runs again and its final receipt must match exactly before publication.
The workflow now executes both passes against real retained PR #324 evidence.
These features do not restore running strategies, exchange connections, orders
or brokerage positions. Exact Mark Price source admission remains blocked
and `PROFITABILITY_NOT_PROVEN` is unchanged.

## 2026-10-19 — Immutable final money receipts

Final `verify_funded_replay_integration` and `recover_funded_money_audit`
JSON receipts now use a shared write-then-atomic-hard-link publication gate.
This removes the check-then-write/replace race for finished evidence files:
existing files (including empty files and symlinks) cannot be overwritten,
unsupported hard-link operations fail closed, and staged JSON is fsynced before
publication. The parent directory is fsynced after publication and staging
cleanup. Checkpoints remain intentionally replaceable for crash recovery, but
never count as independently accepted final money receipts. Negative tests
include a competing writer between the initial check and atomic commit.

The independent 36-model audit, source admission, exchange provenance and
funding-account limitations do not change. The new implementation needs
exact-head CI verification before qualification, and no strategy promotion,
broker integration or production rights are granted.
