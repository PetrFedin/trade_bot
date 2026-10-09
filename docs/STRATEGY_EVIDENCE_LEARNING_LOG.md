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


### 2023-10-09..2026-10-08 fixed six-major portfolio replay

Run: GitHub Actions historical-strategy-evidence-lab #8, exact branch evidence on six
committed daily snapshots (1,096 bars per symbol; 6,576 bars total).

Benchmarks over the synchronized execution window:

- 60% capital-matched equal-weight: +105.9506%;
- equal-weight full capital: +176.5843%;
- BTC buy-and-hold: +186.9284%.

Shipped strategy, USD 10,000 opening cash, 256 closed trades:

| Cost scenario | Return | Ending equity | Max DD | Win rate | Profit factor | Fees |
|---|---:|---:|---:|---:|---:|---:|
| legacy shadow: $0.50/fill + 5 bps slippage/side | +10.7198% | $11,071.98 | 8.8945% | 42.58% | 1.1355 | $256.00 |
| optimistic taker: 6 bps/fill + 3 bps slippage/side | +5.9852% | $10,598.52 | 10.0596% | 41.80% | 1.0756 | $910.24 |
| conservative taker: 8 bps/fill + 5 bps slippage/side | +0.7516% | $10,075.16 | 12.4008% | 40.63% | 1.0094 | $1,184.11 |
| stress taker: 10 bps/fill + 10 bps slippage/side | -8.7346% | $9,126.54 | 16.1405% | 38.67% | 0.8924 | $1,408.05 |

Turnover was approximately 141x–156x opening capital depending on the cost scenario.
All scenarios remain PROFITABILITY_NOT_PROVEN because perpetual funding is not yet
integrated into this portfolio replay.

Interpretation: this is materially weaker than the passive alternatives. Even the legacy
cost model underperformed the capital-matched equal-weight benchmark by roughly 95
percentage points. Under the conservative taker model the strategy preserved nominal
capital but added essentially no economic value over three years while taking trading,
execution and model risk. The stress case loses capital.

This result must not be “repaired” by tuning on the same window. The next test is the
pre-declared broad 21-symbol replay, followed by funding-aware and walk-forward/final
holdout evidence. Strategy changes, if any, must be treated as a new candidate with new
untouched evidence.

### 2023-10-09..2026-06-15 broad 21-symbol portfolio replay

The original 21-symbol liquid-perpetual universe was replayed on the common synchronized
window forced by actual listing history. TONUSDT ends on 2026-06-15, so the broad test
uses 981 common daily bars and does not forward-fill, replace or silently drop TON.

Pre-funding results on USD 10,000 opening cash:

- legacy shadow: -11.7316%, max drawdown 20.9033%, profit factor 0.8587;
- optimistic taker: -13.6571%, max drawdown 22.1107%, profit factor 0.8366;
- conservative taker: -17.9709%, max drawdown 24.8050%, profit factor 0.7878;
- stress taker: -26.4858%, max drawdown 29.9024%, profit factor 0.6893.

The 60% capital-matched equal-weight benchmark returned +17.8020% and BTC buy-and-hold
returned +132.7426% on the same broad synchronized window. The broad strategy is therefore
negative in absolute terms and materially behind passive exposure under every cost scenario.

This strengthens the earlier episode-level finding that the six-major result does not
robustly generalize to the broader universe. A same-window six-major control is required
before attributing the entire difference to universe selection rather than date-window
composition, and has been added to the evidence pipeline. Exact settlement funding is the
next cost layer; these pre-funding numbers remain retained and must not be overwritten.


### Funding-aware portfolio evidence — fixed six-major and broad-21

Official Bybit funding settlements were captured from the public venue endpoint and
hash-locked as immutable snapshots. Actual settlement timestamps are used; the data show
that a universal fixed 8-hour assumption is not valid for every instrument.

The portfolio funding model is deliberately conservative. Daily OHLCV does not reveal
the exact second of an intrabar exit or the exact settlement mark price, so the evidence
reports a timing range using entry execution notional rather than claiming exchange-
statement-exact funding cash flow. Negative funding is not promoted into an optimistic
profitability claim.

Full six-major window, 2023-10-09..2026-10-08:

| Cost scenario | Pre-funding return | Funding-adjusted return range |
|---|---:|---:|
| legacy shadow | +10.7198% | +7.9618% .. +10.2294% |
| optimistic taker | +5.9852% | +3.3177% .. +5.5410% |
| conservative taker | +0.7516% | **-1.8611% .. +0.3193%** |
| stress taker | -8.7346% | -11.2621% .. -9.1639% |

The 60% capital-matched six-major passive benchmark remains +105.9506%. Under the
conservative taker assumptions, funding uncertainty spans both sides of zero: the
strategy is economically indistinguishable from roughly flat while passive exposure
wins by more than one hundred percentage points.

Broad 21-symbol common window, 2023-10-09..2026-06-15:

| Cost scenario | Pre-funding return | Funding-adjusted return range |
|---|---:|---:|
| legacy shadow | -11.7316% | -14.5872% .. -12.1111% |
| optimistic taker | -13.6571% | -16.4889% .. -14.0330% |
| conservative taker | -17.9709% | **-20.7441% .. -18.3279%** |
| stress taker | -26.4858% | -29.1292% .. -26.8239% |

The broad 60% capital-matched equal-weight benchmark is +17.8020%.

Same-window six-major control, also ending 2026-06-15:

| Cost scenario | Pre-funding return | Funding-adjusted return range |
|---|---:|---:|
| legacy shadow | +12.6614% | +10.0794% .. +12.2116% |
| optimistic taker | +8.1751% | +5.6764% .. +7.7699% |
| conservative taker | +3.4185% | **+0.9599% .. +3.0233%** |
| stress taker | -5.4976% | -7.8850% .. -5.8932% |

The same-window six-major 60% capital-matched benchmark is +73.7254%.

Interpretation: the broad-universe failure is not merely an artifact of its earlier end
date. On the identical 981-bar window, six majors remain weakly positive under the
conservative cost model while the broad universe is materially negative. The universe
effect is therefore real, but even the six-major control is economically weak relative
to passive capital-matched exposure. All scenarios remain
PROFITABILITY_NOT_PROVEN.

The next admissible research layer is walk-forward portfolio evidence. No stop/target,
signal, universe or cost parameter may be tuned using these already inspected windows
and later described as untouched out-of-sample evidence.
