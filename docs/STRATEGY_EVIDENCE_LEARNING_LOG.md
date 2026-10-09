# ASTRA Strategy Evidence — Learning Log

This is an append-only research memory for economic evidence. It is deliberately separate
from trading authority and qualification evidence.

## Rules

- Preserve negative results; do not delete or overwrite them after a better-looking run.
- Freeze strategy/config before evaluating an untouched holdout.
- Always report explicit fees, slippage and funding coverage.
- Compare against cash, BTC buy-and-hold and capital-matched equal-weight when applicable.
- A positive absolute return is not edge when a comparable passive benchmark materially wins.
- Keep universe-selection decisions visible; a narrow-universe result must survive a broader universe.
- Record every material model limitation: funding, queue position, partial fills, depth and bar ambiguity.
- Do not relabel a repeatedly inspected period as out-of-sample.
- Parameter searches require multiplicity awareness; the best of many trials is not standalone evidence.
- No research result can grant live trading authority.

## Retained findings

### 2023-09-19..2026-09-17 episode evidence

Six-major daily universe looked better than the broad universe, but robustness failed on expansion.
The fixed out-of-sample six-major episode mean was positive, while the 21-symbol fixed OOS mean was
negative. Parameter tuning underperformed the shipped configuration out of sample.

### Funding study

Funding slightly worsened the broad-universe result. The more important finding was structural:
68.2% of episodes closed in the first bar and mean holding time was 1.51 daily bars. The 2% stop is
inside roughly one daily standard deviation, so a large share of exits are likely noise-driven.

### 2026-03-30..2026-10-06 portfolio control

The legacy fixed-fee shadow model returned +1.856%, but the 8 bps fee/side + 5 bps slippage/side
scenario returned -0.226%. Over the same synchronized window the 60% capital-matched equal-weight
benchmark returned +15.875% and BTC buy-and-hold returned +29.579%.

Interpretation: execution-cost realism erased the small absolute gain and the strategy materially
underperformed passive alternatives. This is negative evidence and must remain part of future
promotion decisions.

## Current test queue

1. Fixed-window three-year six-major portfolio replay.
2. Three-year 21-symbol portfolio replay.
3. Funding-aware portfolio lower/upper bound.
4. Walk-forward portfolio evidence with untouched final holdout.
5. Fee/slippage stress matrix.
6. Regime decomposition.
7. Capital-size/capacity sensitivity.
8. Passive-entry study only after queue/partial-fill assumptions are explicit.
