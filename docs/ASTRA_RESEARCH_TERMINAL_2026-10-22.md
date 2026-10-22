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

## Added net trade-performance analytics

The SAME independently verified closed-trade tape also produces win rate,
mean positive/negative closing P&L, per-closure realized average, Profit Factor
(excluding funding) and a losing-closure streak. Undefined ratios are null
rather than an invented zero or Infinity. Same-day trade ties sort by symbol
and must NOT be interpreted as a real venue execution ordering. The model
has no netting of historical open exposure into trade averages. These are
retrospective descriptive metrics, not forward expected returns.

## Cross-surface navigation

The offline Action artifact bundles the actual verified generated
`docs/status-console.html` as `operator-status.html` beside
`research-terminal.html`. The new terminal navigates to this local
operator authorization state, explicitly labelled as qualified main
rather than confusing historical-model evidence with deployment authority.
The workflow requires `tools/status_dashboard.py --check` before copying it.

## Template separation

The responsive static markup lives in `app/platform_assets/historical_research_terminal.html`.
The Python module performs only verified transformation, security validation
and immutable publication; Ruff checks all executable Python without disabling
long-line diagnostics. Client-side JavaScript only switches existing verified
scenarios, without API calls or order actions.

## Independent visual acceptance and exact source revision

QA on GitHub-generated HTML using Chromium with in-memory markup at 1440,
768 and 375 px established working 6→21 and 8/5→10/10 scenario switches,
matched expected historical returns (+12.82%→−13.60%), no script errors,
no external fetches and no whole-document horizontal overflow. Following
this review, tablet trade panels stack and mobile trades use accessible
compact cards with immediately visible P&L rather than horizontal scrolling.
The GitHub checkout explicitly uses the PR head SHA instead of the default
synthetic merge SHA; the same SHA must be written to branch-head.txt.
