# Trading Quality V2

## Scope

This document records the current trading-quality research boundary for the `trade_bot` project. It is intentionally stricter than a feature checklist: a component being implemented does not mean it is qualified for broker actuation.

## Current decision chain

The research chain is:

1. regime-aware eligibility;
2. optional risk-adjusted cross-sectional ranking;
3. optional correlation diversification of top-K candidates;
4. optional stop-risk / inverse-volatility position sizing;
5. conservative next-open portfolio execution model;
6. hard stop, take profit, trailing stop, optional break-even and profit-protection exits;
7. confirmed re-entry after exits;
8. MFE/MAE/capture/profit-preservation accounting;
9. rolling recent-trade degradation monitoring;
10. same-sample component ablation;
11. non-fitting rolling walk-forward holdouts;
12. read-only external multi-symbol evidence generation.

## Safety invariants

- Live trading is not enabled by this research surface.
- External live order routing is not enabled by this research surface.
- The degradation monitor is not auto-actuated.
- A quality gate may never prevent an exit from an existing position.
- Correlation diversification is optional and is not part of legacy selection unless separately qualified.
- The portfolio gross-exposure percentage is an entry-admission constraint. Market appreciation may move observed gross exposure above the admission threshold; the research model does not silently force deleveraging.
- Intrabar evaluation does not reconstruct an optimistic high/low path. If both protection and take-profit are reachable in an ambiguous bar, protective semantics remain conservative.
- A protective level is not a guaranteed fill. Gap-through exits use the executable open in the OHLCV model.

## Profit-quality interpretation

The project does not use the phrase "close every trade in profit" as a qualification target because it is not achievable under uncertain prices, slippage and gaps.

The measurable target is instead to improve the distribution of outcomes while controlling risk, including:

- win rate;
- profit factor;
- maximum favorable excursion (MFE);
- maximum adverse excursion (MAE);
- MFE capture ratio;
- profit giveback after MFE;
- positive-MFE profit-preservation rate;
- hard-stop frequency;
- consecutive-loss behavior;
- total return and drawdown together rather than either metric alone.

## Evidence hierarchy

### Contract evidence

Unit and synthetic portfolio tests establish deterministic behavior and fail-closed boundaries. They do not establish profitability.

### Same-sample ablation

The V2 comparison separates:

- selection-only;
- sizing-only;
- protection-only;
- combined candidate;
- legacy control.

Confirmed re-entry and conservative intrabar semantics are shared controls and must not be attributed to the candidate.

### Walk-forward holdout

The predeclared walk-forward schedule uses fixed parameters, a historical warm-up window, a subsequent holdout window, non-overlapping holdouts and a fresh portfolio per fold. No fold may fit parameters using its own holdout.

Fold-end equity is a mark-to-market research observation unless an explicit liquidation rule is added and separately qualified. This distinction must be preserved when interpreting realized-trade metrics versus equity-return metrics.

### External multi-symbol evidence

When read-only Alpaca market-data credentials are available, one acquired CSV is hashed and reused for the derived selection, same-sample comparison, walk-forward and diversification reports. Derived reports must match the source CSV SHA-256.

## Degradation monitor boundary

The rolling quality monitor currently produces one of:

- `INSUFFICIENT_DATA`;
- `HEALTHY`;
- `PAUSE_ENTRIES`.

Its thresholds are research parameters and are not calibrated for automatic paper or live control. `allow_exits` remains true for all gate states.

## Correlation diversification boundary

Correlation diversification is measured as a marginal component on top of the combined V2 candidate. Historical correlation is not assumed to be forward-stable. Both same-sample and holdout evidence must be reviewed before considering activation.

## Promotion blockers

Do not promote the V2 decision surface to automatic paper portfolio actuation until all of the following are explicitly resolved:

- full CI and security checks are green on the exact candidate revision;
- real multi-symbol evidence is available and reviewed;
- walk-forward folds contain sufficient actionable observations to support interpretation;
- component ablations do not hide a materially harmful component behind a favorable combined result;
- degradation thresholds are calibrated on data not used to choose those thresholds;
- external paper portfolio execution evidence exists for order creation, fills, partial fills, cancellations, reconciliation, position accounting and exits;
- paper execution confirms that exits remain possible when entry gating is paused;
- no evidence artifact is represented as a guarantee of future profitability.

## Next implementation priority

After CI stabilization and evidence review, the next engineering layer is paper-only portfolio actuation and observation. It should consume qualified decision outputs through the existing OMS/risk/execution boundaries, preserve all exit paths, produce durable order/fill evidence, and remain incapable of live routing until a separate promotion decision is made.
