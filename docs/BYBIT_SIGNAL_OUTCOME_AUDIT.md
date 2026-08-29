# Bybit Signal Outcome Audit

## Purpose

This research boundary answers a narrower question than strategy promotion:

> Given the frozen strategy contract, how did every eligible signal move afterwards, which signals became executable plans, how did actually opened trades close, and is there any cross-token pattern that historically closed positively every time with enough support to deserve independent validation?

The answer from the first immutable seven-day evidence snapshot is **no supported always-working pattern has been found**.

This does not weaken the execution safety stack. It prevents retrospective pattern mining from silently becoming trading policy.

## Frozen evidence snapshot

Source run: `33248448689`

Source head: `931ee7db3ded79b4574485bb0dbf075e29cea104`

Source: official Bybit public raw trade archives aggregated to completed 5-minute bars.

Completed UTC dates:

- 2026-08-22
- 2026-08-23
- 2026-08-24
- 2026-08-25
- 2026-08-26
- 2026-08-27
- 2026-08-28

Frozen symbols:

`BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, BNBUSDT, DOGEUSDT, LINKUSDT, ADAUSDT`

The compact immutable result is stored in:

`research/bybit_signal_outcome_snapshot_2026-08-22_2026-08-28.json`

That file also records the SHA-256 digest of every per-symbol GitHub Actions artifact.

## Two different populations must remain separate

### 1. Every eligible signal

This population ignores portfolio slot competition and cooldowns. A signal becomes knowable only after its decision bar completes. Forward movement starts at the next contiguous 5-minute bar open.

The audit records:

- 15m, 60m and 240m directional movement;
- 240m MFE/MAE only with a complete 48-bar future window;
- frozen signal quality score;
- quality-to-entry-gate ratio;
- descriptive clarity band;
- plan eligibility at the fixed 1,000 USDT reference equity;
- economics block reasons.

Observed population:

- 1,438 eligible signals;
- only 452 (31.43%) formed an eligible plan at the fixed reference equity;
- median quality/gate ratio: about 3.29x;
- positive directional movement: 42.91% at 15m, 34.87% at 60m, 38.93% at 240m;
- average directional return was negative at all three horizons;
- median 240m MFE was about 2.52R while median 240m MAE was about 3.55R.

The important implication is that signal eligibility and visual/score strength do not by themselves identify a stable directional edge.

### 2. Actually opened and closed trades

This population uses the existing qualified trade-management semantics and therefore includes plan economics, actual entry eligibility, protection, stop and exit behavior.

Observed population:

- 156 closed trades;
- 68 positive closes (43.59%);
- 52 planned target/profit-protection exits (33.33%);
- total modeled net PnL: approximately -218.43 USDT.

Exit attribution:

| Exit reason | N | Positive close rate | Total modeled net PnL |
| --- | ---: | ---: | ---: |
| HARD_STOP | 65 | 0% | -644.09 USDT |
| BREAK_EVEN_STOP | 37 | 43.24% | -46.67 USDT |
| NET_TARGET | 22 | 100% | +440.00 USDT |
| PROFIT_PROTECTION | 32 | 93.75% | +32.33 USDT |

The principal loss mechanism is therefore not hidden in late exits. It is the high number of trades that move against the entry quickly enough to hit the hard stop. Break-even exits also remain slightly negative after modeled costs. Profit protection succeeds at protecting many trades, but historically captures a small amount of net PnL relative to the favorable excursion that preceded it.

## Signal clarity is not monotonic edge

The first audit deliberately does not fit a new quality threshold. The following quality/gate bands are descriptive only.

| Quality / frozen entry gate | N | Positive 240m direction | Avg 240m directional return |
| --- | ---: | ---: | ---: |
| 1.00–1.25x | 43 | 35.7% | -0.377% |
| 1.25–1.75x | 135 | 45.2% | -0.317% |
| 1.75–3.00x | 436 | 41.8% | -0.332% |
| 3.00–5.00x | 604 | 39.6% | -0.400% |
| >=5.00x | 220 | 28.31% | -1.065% |

The most extreme quality scores were historically worse, not better. This is consistent with an overextension/exhaustion hypothesis, but the audit does **not** make a causal claim and does not invert the strategy automatically.

## Cross-token pattern search

Frozen pattern grain:

`side | volatility regime | trend regime | breakout regime | turnover regime`

Minimum historical support used for the first search:

- at least 5 closed trades;
- at least 2 different tokens.

Results:

- supported 100% positive-close patterns: **0**;
- supported 100% planned-profit-exit patterns: **0**.

Therefore there is currently no defensible answer of the form “this historical signal combination always works.”

### Best observed candidate, not a rule

`LONG | VOL_LOW_NORMAL | TREND_MODERATE | BREAKOUT_PULLBACK | TURNOVER_LOW`

Observed:

- 13 trades;
- 4 tokens: ADA, DOGE, SOL and XRP;
- 10/13 positive closes = 76.92%;
- 7/13 planned profit exits = 53.85%;
- modeled net PnL +71.59 USDT;
- profit factor about 3.39;
- 95% Wilson lower bound for positive-close rate only about 49.7%.

This is the strongest candidate for independent validation, but the sample is too small and is not perfect.

### Strong counterexample to “clearer is safer”

`SHORT | VOL_LOW_NORMAL | TREND_STRONG | BREAKOUT_CONFIRMED | TURNOVER_HIGH`

Observed:

- 5 trades across 4 tokens;
- 0/5 positive;
- approximately -42.88 USDT;
- median quality/gate ratio around 6.07x.

This is why increasing the quality threshold from the same retrospective window is not an acceptable fix.

## What is not changed

This research work does not change:

- EMA, momentum, breakout, ATR or turnover parameters;
- LONG/SHORT policy;
- ranking weights;
- risk fraction or notional limits;
- runner admission;
- protection parameters;
- Demo ARM/HALT or approval semantics;
- mainnet capability.

No order path is added.

## Required next evidence gates

### Gate 1 — non-overlapping historical holdout

`app/strategy/crypto_signal_pattern_holdout.py` requires a pattern to be discovered with support first and then repeat unchanged on a non-overlapping holdout population.

A single holdout failure breaks historical perfection. A single-token 5/5 is not cross-token evidence. A positive break-even close cannot be relabeled as a planned target/protection success.

### Gate 2 — richer exact-cell context

Price-shape patterns alone are not sufficient. The existing v111/v112/v117/v118 research architecture must add point-in-time:

- open-interest regime;
- holder crowding;
- prior funding;
- stress regime;
- coverage-qualified liquidation context where available.

Those dimensions must be known at or before the source signal time.

### Gate 3 — prospective confirmation

Any pattern that survives historical discovery/holdout must then be checked against immutable future outcomes already supported by the prospective evidence stack. Strategy promotion remains forbidden until the same frozen conditions accumulate enough independent forward observations.

## Current decision

The correct current action is **not** to search for a higher quality-score threshold and not to auto-promote BNB's 2/2 or BTC's 1/1 observations.

The correct next unit is:

`frozen historical discovery -> non-overlapping holdout -> exact OI/crowding/funding/stress cell -> prospective v112-v118 confirmation -> only then a separate strategy-change experiment`
