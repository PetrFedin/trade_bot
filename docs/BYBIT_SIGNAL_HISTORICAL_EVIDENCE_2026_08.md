# Bybit signal historical evidence — August 2026

Status: research-only, frozen evidence memo. No strategy promotion or execution authorization.

## Decision question

For the frozen USDT-perpetual universe
`BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, BNBUSDT, DOGEUSDT, LINKUSDT, ADAUSDT`, determine:

1. how every eligible signal moved after it became knowable;
2. how clear the signal was under the existing frozen score;
3. which signals became cost-aware trade plans at 1,000 USDT reference equity;
4. how the actual shared-capital replay trades closed;
5. whether any repeatable pre-entry price-only condition historically reached the canonical +20 USDT target before the hard stop every time;
6. what must be tested next without fitting a new rule to the same outcomes.

## Frozen evidence

### 14-day signal / portfolio audit

- Source: official completed Bybit public trade archives, aggregated to 5m.
- Completed UTC dates: 2026-08-16 through 2026-08-29.
- Exact source head: `eb72a18c99e3d1229d7b7ee3d67efe4555f04386`.
- Workflow: `33341570731`.
- Artifact: `bybit-signal-outcomes-14d-33341570731`.
- Artifact SHA-256: `3f931f8d0ff66a0f24261a0786a56e2a33f374ba3b3d4c01f7097ff648ba5c70`.

### 14-day target-before-stop audit

- Same completed UTC archive window.
- Exact source head: `263933cb8a067a59aa13267e889f72bc926dcbbe`.
- Workflow: `33342281245`.
- Artifact: `bybit-signal-first-touch-14d-33342281245`.
- Extracted report SHA-256: `91abfb6ab3562f968b20cf4bfe9562bff0123c0971f79a61d1bc40cd69097e63`.

The two audits answer different questions. Forward directional movement measures what price did after a signal. First-touch measures the stricter path question: after canonical next-open entry, did the +20 USDT target occur before the canonical hard stop?

## All eligible signals

There were **2,394** eligible completed-bar signals. At a fixed 1,000 USDT reference equity, **792** were cost-aware plan-eligible (33.08%).

| Token | Eligible signals | Plan-eligible | Plan pass rate | 240m positive direction | Median quality / gate | Median 240m MFE | Median 240m MAE |
|---|---:|---:|---:|---:|---:|---:|---:|
| ADA | 149 | 142 | 95.30% | 39.60% | 3.80x | 2.54R | 3.27R |
| BNB | 22 | 4 | 18.18% | 4.55% | 6.15x | 0.99R | 3.88R |
| BTC | 379 | 13 | 3.43% | 49.34% | 3.27x | 3.03R | 2.88R |
| DOGE | 338 | 160 | 47.34% | 41.72% | 3.80x | 2.90R | 2.90R |
| ETH | 354 | 26 | 7.34% | 44.35% | 3.07x | 2.64R | 3.12R |
| LINK | 51 | 48 | 94.12% | 23.53% | 4.65x | 1.63R | 3.61R |
| SOL | 521 | 92 | 17.66% | 48.06% | 3.20x | 2.81R | 3.03R |
| XRP | 580 | 307 | 52.93% | 47.23% | 3.41x | 2.83R | 2.96R |

This table exposes the fixed-dollar feasibility bias: ADA and LINK almost always pass the 1,000 USDT plan gate, while BTC and ETH rarely do. The plan gate therefore changes the token mix materially before portfolio ranking starts.

## Signal clarity is not a profitability ordering

Across all eligible signals, 240-minute positive-direction rates were:

- `CLEAR`: **49.04%**;
- `STRONG`: **44.99%**;
- `MARGINAL`: **38.46%**.

Across independent target-before-stop episodes:

- `CLEAR`: 13 / 51 = **25.49%**;
- `STRONG`: 119 / 543 = **21.92%**;
- `MARGINAL`: 5 / 23 = **21.74%**.

A higher scalar quality score is therefore not a monotonic success measure. Raising the current quality threshold is not supported by this evidence.

## Independent target-before-stop episodes

The first-touch audit starts with 792 plan-eligible signal rows and de-duplicates uninterrupted 5m runs sharing exact `symbol|side|price-pattern`. This produces **617 independent episodes**:

- TARGET_FIRST: **137**;
- STOP_FIRST: **471**;
- NEITHER: **9**;
- AMBIGUOUS_SAME_BAR: **0**;
- INCOMPLETE: **0**;
- TARGET_FIRST rate: **22.20%**;
- median quality / gate: **3.45x**;
- median expected net edge: **21.79 USDT**;
- median 240m MFE: **2.38R**;
- median 240m MAE: **2.99R**.

| Token | Episodes | Target first | Stop first | Neither | Target-first rate |
|---|---:|---:|---:|---:|---:|
| BNB | 3 | 1 | 2 | 0 | 33.33% |
| DOGE | 127 | 33 | 93 | 1 | 25.98% |
| BTC | 8 | 2 | 5 | 1 | 25.00% |
| XRP | 236 | 58 | 174 | 4 | 24.58% |
| ADA | 102 | 24 | 78 | 0 | 23.53% |
| ETH | 23 | 5 | 17 | 1 | 21.74% |
| LINK | 39 | 5 | 33 | 1 | 12.82% |
| SOL | 79 | 9 | 69 | 1 | 11.39% |

No token is an invariant winner. Small BNB/BTC/ETH samples are not treated as evidence of an edge.

### Side asymmetry

- LONG: 105 / 387 = **27.13%** target first.
- SHORT: 32 / 230 = **13.91%** target first.

The current SHORT signal family is materially weaker and must not be treated as a symmetric mirror of LONG.

## Path order is a major defect

Of the **471 STOP_FIRST** independent episodes, **315 later reached at least 1R favorable excursion** and **204 later reached at least 2R** within the same 240-minute window.

Therefore many failures are not simply wrong eventual direction. The market frequently hits the current hard stop before later moving strongly in the forecast direction. This points to entry timing / overextension / regime context as a major research target.

The median modeled hard-stop loss in this sample is approximately 9.75 USDT versus a fixed +20 USDT target. A rough target-first break-even probability is therefore about 32.8% before more complex protection/gap effects. The observed 22.2% is below that level; LONG at 27.1% is also below it and SHORT at 13.9% is much worse.

## Actual shared-capital portfolio trades

The 14-day canonical replay accepted and closed **102** trades:

- 36 economic wins;
- 11 breakeven;
- 55 losses;
- modeled PnL **-176.672724 USDT**;
- final equity **823.327276 USDT** from 1,000;
- max drawdown **20.79%**;
- fees **120.277718 USDT**.

| Token | Trades | Win | BE | Loss | Total modeled PnL |
|---|---:|---:|---:|---:|---:|
| ADA | 26 | 8 | 1 | 17 | -113.88 |
| BNB | 1 | 1 | 0 | 0 | +0.66 |
| BTC | 2 | 1 | 0 | 1 | -9.18 |
| DOGE | 12 | 4 | 2 | 6 | +7.04 |
| ETH | 3 | 2 | 0 | 1 | +30.52 |
| LINK | 8 | 3 | 1 | 4 | -6.26 |
| SOL | 9 | 1 | 1 | 7 | -55.30 |
| XRP | 41 | 16 | 6 | 19 | -30.29 |

Positive PnL on BNB, DOGE and ETH is not a robust token-selection result because the samples are respectively 1, 12 and 3 trades and no token produced invariant target-first behavior.

### Exit attribution

| Exit reason | Count | Economic outcome | Total PnL | Median MFE | Interpretation |
|---|---:|---|---:|---:|---|
| HARD_STOP | 51 | 51 losses | -464.25 | 0.025R | dominant entry failure; almost no favorable excursion |
| BREAK_EVEN_STOP | 13 | 11 BE, 2 losses | -4.31 | 0.91R | protection roughly preserved capital |
| NET_TARGET | 13 | 13 wins | +260.00 | 1.99R | canonical target worked when reached |
| PROFIT_PROTECTION | 25 | 23 wins, 2 losses | +31.89 | 1.70R | usually captured developed edge; two giveback/gap exceptions remain |

The largest loss source is therefore not a generally broken exit engine. It is the population of entries that proceed directly to hard stop with almost no favorable excursion.

## Price-only repeated-pattern result

Predeclared pattern identity:

`side | clarity | configured-volatility-third | trend>=1ATR | breakout-confirmed-vs-pullback`

Support gates:

- at least 5 independent episodes;
- at least 2 symbols;
- at least 3 distinct UTC days;
- no post-outcome threshold fitting.

Qualified cross-token patterns: **18**.

Historical-perfect TARGET_FIRST patterns: **0**.

Best supported group:

`SHORT|MARGINAL|VOL_LOW_NORMAL|TREND_MODERATE|BREAKOUT_PULLBACK`

- N=7;
- 2 symbols;
- 4 days;
- 3/7 = 42.86% target first;
- Wilson 95% lower bound 15.82%.

It is not remotely sufficient to claim an always-working signal.

### Strong negative regimes

`SHORT|STRONG|VOL_LOW_NORMAL|TREND_STRONG|BREAKOUT_CONFIRMED`

- N=24;
- 6 symbols;
- 7 days;
- 1/24 = **4.17%** target first;
- median quality / gate about **5.54x**;
- later half 0/12.

This is evidence that a very high current quality score can describe overextension rather than entry quality.

`LONG|CLEAR|VOL_LOW_NORMAL|TREND_MODERATE|BREAKOUT_CONFIRMED`

- N=6;
- 4 symbols;
- 5 days;
- 0/6 target first.

The second group is still small and remains a research veto candidate, not a production rule.

## Main conclusion

**No real supported price-only signal set in the current frozen family historically always closed as required.**

This conclusion is stronger than a portfolio win-rate statement because it survives removal of concurrency/cooldown competition and is evaluated on every plan-eligible independent episode.

The data also rejects three tempting shortcuts:

1. do not promote a token because its tiny portfolio sample is positive;
2. do not raise the scalar quality threshold and call `STRONG` safer;
3. do not optimize only the exit engine when 51 hard-stop trades had almost no favorable excursion.

## Next evidence gate: pre-entry derivatives context

PR #100 adds a research-only join of point-in-time:

- open-interest regime;
- account-ratio crowding;
- prior settled funding sign;
- frozen stress regime.

The join rejects derivatives observations timestamped after the signal decision. It does not change the strategy.

### Current acquisition status

The deterministic #100 contract is green. GitHub-hosted public Bybit V5 acquisition is blocked by Bybit/CloudFront HTTP 403 with the explicit message that access is blocked from the runner country. Therefore no derivatives-enriched historical result is claimed yet.

A protected manual `[self-hosted, bybit-demo]` fixed-egress research job is prepared. It references no Bybit credential secrets, refuses to run if trading/read-only credential variables are present in process environment, and exposes no ARM/HALT/order path. The job must remain a separate evidence-acquisition gate; the 403 is not bypassed with third-party derivatives data.

If a derivatives-enriched retrospective 100% group later appears, it remains only a candidate. It must be frozen into an immutable identity and falsified prospectively through the existing OOS evidence chain before any shadow strategy or rule change is considered.

## Safety / interpretation boundary

- `retrospective_only=true`
- `parameter_retuning_performed=false`
- `ranking_weights_changed=false`
- `strategy_selection_allowed=false`
- `strategy_promotion_allowed=false`
- `trade_actionable=false`
- `demo_activation_allowed=false`
- `live_activation_allowed=false`
- `bybit_live_order_routing_allowed=false`
- `predictive_guarantee_allowed=false`
