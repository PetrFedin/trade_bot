# ASTRA — Research Terminal v1 / 2026-10-22

## Objective
Replace demo-like financial dashboards with a real offline, read-only,
professionally styled research interface grounded only in audited model
results. This is NOT an interactive live trading desk, live market stream,
financial projection, or strategy approval surface.

## Data contract
`daily_funding_cash_replay` (36 original historical models) → original
`verify_daily_funding_report` → immutable 36-model
`historical_execution_journal` → independent rebuild/check inside
`historical_research_console` → atomic no-replace HTML publication.

The console refuses stale or tampered reports, any unexpected model, incomplete
scenario matrix, nonfinite metrics, inconsistent equity/return, corrupted
symbol count, unqualified matched six-vs-21 pairs and any promotions.
Every time a scenario is selected, the view recomputes from the compact
read-only payload: universe, fee/slippage, exit convention and funding scenario.

## Screens
- Historical equity return / USDT ending equity / daily-close drawdown / closed trades.
- 90-day contiguous equity-path return bars, partial windows identified.
- Funding/cost breakdown; capital-matched six-vs-21 comparison on identical dates.
- By-symbol trade attribution and exit reason census.
- Worst and best closed trade tables with UTC entry/exit and reasons.
- Full source, original journal and independent auditor SHA-256.
- Fully responsive 375/768/1440 CSS; no external resources, artificial counters,
  mock trading fills, API calls or execution controls. Accessible native select.

## Risk and integrity
The analysis is historical development evidence, previously inspected and
not untouched OOS. Exchange execution, exact settlement Mark Price, intraday
liquidations, queue position and partial fills remain unverified. Daily funding
uses entry notional and cash-booking approximations. 90-day capital returns
cannot be added to realized closed trade PnL because open holdings overlap.
The generated HTML never grants trade authority; its runtime only filters
frozen evidence; it is a downloadable GitHub Actions report, NOT a published
live web application. `PROFITABILITY_NOT_PROVEN`.

## CI acceptance
Ruff + synthetic adversarial tests + full genuine-source replay + separate
money audit + full journal generation + byte-identical regenerated HTML.
All outputs are uploaded together with exact branch SHA. Missing evidence
is a hard failure. UI is not automatically published or deployed.
