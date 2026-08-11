# Paper Trading Quality E2E

## Purpose

This document defines the current paper/shadow boundary for the cross-sectional trading-quality strategy. It is an engineering contract, not a profitability claim and not an authorization for live trading.

The system deliberately separates three questions:

1. **Signal quality** — was the symbol a good candidate to enter?
2. **Position-management quality** — after entry, how much favorable excursion was preserved or given back?
3. **Execution quality** — how did exact broker fills compare with the effective submitted limit?

A strong result in one layer must not be used to hide a weak result in another.

## Decision-to-fill path

The intended paper path is:

`multi-symbol OHLCV -> ranked selection -> optional shadow entry-quality filter -> durable re-entry confirmation -> risk-aware sizing -> target plan -> quality entry gate -> batch pre-trade risk -> strategy intent ownership -> durable OMS outbox -> paper broker executor -> exact fill accounting -> strategy-scoped observers -> re-entry/trade quality/execution quality`

Protection is a parallel exit path:

`fresh observed price -> durable position protection -> risk check -> strategy intent ownership -> protective SELL outbox -> adverse-only durable REPLACE when stale -> exact fill -> attributed trade-quality close`

Neither application path directly sends a live order.

## Non-negotiable invariants

### Exits remain possible

The rolling strategy-quality gate may pause **new entries only**. It must never veto an exit. Core account/risk safety controls can still reject an invalid order, but degradation of strategy metrics is not allowed to trap an open position.

### A planned SELL does not finance a BUY

Cash and gross exposure released by an unfilled SELL are not available to a same-cycle BUY. Portfolio admission uses current durable positions and cash, not hypothetical post-fill state.

### One strategy cannot contaminate another strategy's quality evidence

`SQLitePaperStrategyIntentRegistry` durably records the owner of an approved paper intent before OMS outbox persistence. `StrategyScopedPaperFillObserver` forwards an exact fill only when ownership, symbol and side agree.

The account-level ledger still sees every fill. This is intentional: foreign strategy positions must affect cash and gross-risk calculations even though they must not affect this strategy's re-entry, PnL-quality or execution-quality statistics.

### Fill replay is expected

Portfolio fills, re-entry events, trade-quality fills and execution-quality fills are idempotent. Exact-fill observers run on replay even when the portfolio event already exists, allowing a restart to repair a crash window after portfolio persistence but before downstream strategy state was updated.

### Re-entry confirmation is fill-based

A SELL fill arms re-entry confirmation. A BUY fill clears it. Preparing, cancelling or rejecting an order does not pretend that a position changed. Decision timestamps are deduplicated so repeated evaluation of one signal bar cannot manufacture a confirmation streak.

### Profit protection is not a promise of a profitable fill

Break-even, profit-protection and trailing logic define protective thresholds. A gap, latency, liquidity shortage or adverse fill can still produce an exit below the threshold or below entry cost. The correct target is measured reduction in favorable-excursion giveback, not a guarantee that every trade closes positive.

## Position-management evidence

`PaperTradeQualityTracker` reconstructs position episodes from exact durable fills in broker timestamp order. It tracks:

- exact entry cash out including fees;
- exact exit cash in net of fees;
- net PnL and return;
- observed maximum favorable and adverse excursion;
- MFE capture ratio and giveback;
- final exit intent and exit reason;
- replacement/partial-fill sequences;
- late delivery of an earlier partial fill.

MFE/MAE use only prices actually supplied to the tracker plus exact fill prices. They do not claim knowledge of unseen intraperiod highs or lows.

Fresh reference prices are recorded from both protection cycles and ordinary cross-sectional decision cycles so `SELECTION_EXIT` and protection exits use the same observed-excursion accounting.

## Protective SELL execution

A pending protective exit is persisted before broker submission. Once the broker order is active, `PaperProtectionRepricePlanner` may request an adverse-only durable `REPLACE` when the observed price moves materially below the effective SELL limit.

Rules:

- no second independent SELL while the original order is active;
- only one active mutation at a time;
- no upward chasing when price improves;
- effective limit incorporates the last confirmed replacement;
- partial fill plus confirmed cancel can reissue only the verified remaining quantity;
- an OMS `FILLED` order with a still-open local ledger fails closed until reconciliation instead of creating another SELL.

The existing at-most-once mutation executor remains responsible for the broker PATCH and ambiguous-result recovery.

## Execution-quality evidence

`PaperExecutionQualityTracker` compares every exact scoped fill against the effective submitted limit.

Signed slippage is normalized so:

- **positive** = worse execution;
- **negative** = better execution;
- BUY and SELL use the same interpretation.

The tracker records per-fill fraction, basis points and dollar slippage and can aggregate weighted execution quality separately for entries and exits. A successfully replaced protective SELL uses the confirmed replacement limit as its baseline, not the stale original order price.

This is deliberately separate from trade PnL. A profitable trade can have poor exit execution, and a losing trade can still have excellent execution relative to the submitted order.

## Entry-quality shadow candidate

`EntryQualityFilteredSelector` is not promoted into the active combined strategy. It is a shadow wrapper over the existing ranked selector and currently tests three transparent entry-path hypotheses:

- minimum trend efficiency to reject choppy paths;
- maximum extension from the recent mean to avoid late chasing;
- maximum single-bar positive return to reject one-bar spike momentum.

An average-dollar-volume floor exists as an optional execution guard but is disabled in the initial shadow policy until capital size and market-data coverage are calibrated.

The filter may promote the next base-ranked eligible candidate when a higher-ranked symbol is blocked. It does not rewrite the underlying candidate score.

Promotion remains blocked until non-overlapping walk-forward comparison and real paper selection/fill evidence exist. The filter can miss genuine fast breakouts; lower turnover alone is not evidence of better trading.

## Current promotion boundary

The trading-quality stack remains paper/shadow evidence. No component described here should be used to infer guaranteed profitability or to silently enable external live routing.

Before any broader promotion, evidence should include:

- clean CI and deterministic replay/restart tests;
- real multi-symbol paper decisions and exact fills;
- observed entry/exit slippage distributions;
- rolling trade-quality metrics with calibrated thresholds;
- non-overlapping walk-forward evidence for any new selection filter;
- explicit review of gaps, rejected/cancelled orders, partial fills and reconciliation cases.
