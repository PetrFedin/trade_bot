# ASTRA historical trade journal — 2026-10-20

Research-only companion to frozen PR #324 source and 36-model full historical
cash-feedback study (the base branch is not changed).

## Why
Report how the *model* would have traded on its already inspected genuine
historical price/funding archives, not just its terminal PnL. Trace entries,
exits, exits by reason, cost decomposition, 90-day non-overlapping model
equity returns, best and worst completed trades and matched six-vs-21 comparisons.

## Non-negotiable controls
- Replay the original snapshot corpus and independent funding/cash verifier.
- Refuse source digest drift, foreign/modelled authorizations, missing models,
  missing/duplicated decision dates, false entry-selection linkage, corrupt
  trade fee/PnL math, non-UTC dates and mismatched funding by booking day.
- Keep real chronological simulation untouched: analysis is a POST-replay
  consumer; neither policy/selection/stop/target nor any order route changes.
- Only compare six/broad21 when dates are identical; do not extrapolate the
  2026-06-15 funding-incomplete day.
- Full 90-day windows and partial tails are labelled separately, never treated
  as independent out-of-sample trials. Closed-trade profits by EXIT DAY cannot
  be added to capital returns because mark-to-market holdings overlap.
- Require modelled fills, venue mark settlement, capital/margin truth and future
  untouched holdout to remain explicitly unverified. No strategy promotion.

## Verification
GitHub Actions historical-trade-journal replays the pinned 36-model suite,
runs the existing independent financial auditor and outputs the full JSON journal
plus SHA-256 in a 30-day workflow artifact. Red CI or missing source are blockers.

## Interpretation
Already inspected periods are descriptive evidence only.
Funding is constant entry-execution notional and daily-booked, not actual
Bybit account ledger. 60-minute source first-touch is not a queue, latency,
spread, partial-fill or within-hour venue-execution replay. The 2026-10-20
journal cannot establish profitability; PROFITABILITY_NOT_PROVEN remains.

## Immutable journal output / parallel writer safety

The final 90-day journal reuses exactly the SHA-pinned `tools/publish_research_receipt.py`
immutable JSON publisher from draft PR #326 (Git blob `5cfa3cc54c20dc983b32634221585d161c2df174`).
It writes and fsyncs a same-directory temporary file, atomically creates the final
name via hard link with no replacement, then fsyncs the parent directory. The
seven existing adversarial receipt tests from the research-source branch are
included unchanged (destination symlinks, pre-existing empty files, racing writer,
missing hard-link support, symlink parent and NaN). Unsupported filesystems
fail closed rather than falling back to `exists()+write_text()`.
