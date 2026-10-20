# ASTRA — comparative functionality and causal replay QA, 2026-10-20

Research-only increment on top of PR #328. No strategy promotion, production or demo trading.

| Benchmark | Existing published feature | ASTRA disposition |
|---|---|---|
| Veles (RU) | DCA/Grid, signals, TradingView and backtest visual analytics | Research a safe UI/MAE-MFE; no uncontrolled DCA |
| T-Invest API (RU) | Sandbox, market data and portfolio API | Future separate adapter qualification, no connection now |
| Finam API (RU) | Orders, market data, portfolio data | Reference-only adapter conformance |
| Freqtrade | Lookahead-analysis and recursive-analysis | ADOPT bounded no-future-data replay check in this PR |
| Hummingbot V2 | Modular controllers/executors | ADAPT independent research strategy controllers behind common risk |
| QuantConnect LEAN | Event engine, portfolio/risk and research | ADAPT event-lineage and benchmark validation |
| NautilusTrader | Backtest/live execution semantics | REFERENCE; execution parity remains unproven |
| TradingView | Bar Magnifier and historical order simulation | ADAPT realistic intrabar evidence and uncertainty |
| 3Commas, Bitsgap | DCA/Grid, settings, dashboards and backtests | UI/reference and gated candidate studies |

Official documentation links:
https://docs.freqtrade.io/en/stable/lookahead-analysis/
https://docs.freqtrade.io/en/stable/recursive-analysis/
https://hummingbot.org/strategies/v2-strategies/controllers/
https://www.lean.io/
https://www.tradingview.com/support/solutions/43000669285-what-is-bar-magnifier-backtesting-mode/
https://developer.tbank.ru/invest/intro/developer/sandbox
https://api.finam.ru/
https://help.veles.finance/
https://help.3commas.io/
https://bitsgap.com/

## P0 implemented
Pinned source and independent 36-ledger audit → uncached original selector →
full period vs three truncated prefixes (25%, 50%, 75%) across three
chronologies, two fee/slippage pairs, two exit/funding modes. Checks exact
past decisions, trade economics, daily equity, funding and hourly first-touch.
36 comparisons; fail closed on mismatch; immutable JSON result.

## Research limitations
This is bounded evidence, not exhaustive absence-of-lookahead proof. Historical
2023–2026 data were already inspected. Cash-funded model uses entry notional
rather than verified exchange Mark Price, and 60m OHLC is not true fill order.
No broker connectivity or trading permission. PROFITABILITY_NOT_PROVEN.

## Next queue
P0: historical Mark Price provenance, venue fill microstructure, complete CI.
P1: per-coin regime and risk-relative attribution; MAE/MFE with coverage flags.
P1: separate bounded DCA/Grid/mean reversion candidates, no auto-winner.
P2: RU/EN dashboard and evidence playback, export and approval roles.
