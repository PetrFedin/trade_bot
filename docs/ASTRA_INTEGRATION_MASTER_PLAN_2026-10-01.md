# ASTRA — Integration & Assurance Master Plan

**Document:** `docs/ASTRA_INTEGRATION_MASTER_PLAN_2026-10-01.md`  
**Status:** PLANNED  
**Date:** 2026-10-01  
**Repository:** `PetrFedin/trade_bot`

## Purpose

Canonical plan for strengthening ASTRA as a fail-closed, evidence-driven trading platform. This plan does **not** add a new trading strategy or authorise live trading. Engineering qualification, strategy promotion and trading authority remain separate gates.

## Current authority to preserve

ASTRA already owns market-data truth, deterministic risk, OMS/outbox, account and portfolio snapshots, reconciliation/recovery, broker/provider boundaries, qualification gates and paper/external-readonly contours.

Green code does not prove profitability.

## Integration disposition

| Capability | Source | Decision | Boundary |
|---|---|---|---|
| Historical exchange replay | native/current replay work | ADOPT/DEEPEN | drives same normalized path as live |
| Adapter conformance | CCXT patterns | REFERENCE/ADAPT | never direct execution authority |
| Property-based risk tests | Hypothesis | ADOPT | CI/qualification only |
| Symbolic execution | CrossHair | ADOPT | pure risk/accounting functions |
| Mutation testing | mutmut | ADOPT | critical authority modules |
| Static policy rules | Semgrep | ADOPT | architecture gate |
| Strategy experiment registry | MLflow | ADAPT | research evidence only |
| Policy as code | OPA | ADAPT | allow/deny evidence, no order submit |
| Supply-chain attestation | in-toto | ADOPT | build/qualification provenance |
| Fault injection | Toxiproxy/Litmus patterns | ADAPT | controlled test contour |
| Alternative trading engine | Nautilus Trader | REFERENCE ONLY | no replacement of ASTRA authority |

## Phase 0 — Authority gate

Before any new integration:

1. preserve ARM/HALT and dispatch boundaries;
2. preserve separation of paper/mainnet/live;
3. keep `PROFITABILITY_NOT_PROVEN` until evidence changes;
4. ensure no CI/research tool can enable execution;
5. keep secrets and signer identity outside research artefacts.

## Phase 1 — Historical Exchange Replay

Deepen the existing replay harness so every replay record includes:

- raw provider event;
- receive timestamp;
- exchange timestamp;
- sequence/order;
- normalized event;
- instrument-spec version;
- continuity/gap marker;
- replay dataset ID.

Replay should drive the same normalization -> strategy target -> risk -> OMS/accounting path as live wherever technically possible.

A production incident must be reproducible from a versioned replay dataset plus code/config SHA.

## Phase 2 — Adapter Conformance

Use CCXT only as a vocabulary/reference source.

Each exchange adapter gets fixtures for:

- instrument/tick/lot precision;
- market status;
- order status mapping;
- fills/trades;
- cancel/reject;
- rate limits;
- timestamps;
- duplicate/out-of-order events.

A new adapter does not enter paper contour before conformance passes.

## Phase 3 — Property-Based Risk Tests

Use Hypothesis for invariants:

- exposure cannot exceed configured limit;
- pending exposure reserves exactly once;
- duplicate fill cannot duplicate accounting;
- cancel/reject/fill transitions conserve quantity;
- invalid/NaN/overflow values fail closed;
- rounding follows instrument specification;
- stale snapshots cannot silently authorize risk.

## Phase 4 — Symbolic + Mutation Assurance

### CrossHair
Apply to pure deterministic risk/accounting/state-transition functions.

### mutmut
Critical mutations must be killed when:
- inequality flips;
- HALT check disappears;
- duplicate guard disappears;
- exposure reservation is skipped;
- account reconciliation branch is inverted.

Do not chase a cosmetic 100% score across non-authority code.

## Phase 5 — Semgrep Architecture Rules

Create repository-specific rules preventing:

- broker submit outside dispatch authority;
- direct portfolio mutation;
- bypass of pre-trade risk;
- direct live flag enablement;
- unsafe secret logging;
- non-deterministic time/random use inside accounting authority.

## Phase 6 — Strategy Experiment Registry

Use MLflow or equivalent only for research evidence.

Record:

- replay/dataset ID;
- code SHA;
- strategy/config version;
- costs/slippage assumptions;
- train/validation/test periods where applicable;
- output metrics;
- artefacts.

No "best run" may auto-promote a strategy.

## Phase 7 — Policy as Code

OPA can evaluate qualification/release policy:

- required tests present;
- replay suite passed;
- reconciliation passed;
- environment allowed;
- signers/approvals valid;
- artefact attestation present.

OPA returns allow/deny evidence. It never places orders.

## Phase 8 — Supply-Chain Attestation

Use in-toto to attest:

`source SHA -> build -> tests -> replay qualification -> artefact -> deploy`

This proves deployed artefacts correspond to qualified source.

## Phase 9 — Fault Qualification

Use Toxiproxy and/or Litmus-style scenarios for:

- latency;
- disconnect;
- duplicate messages;
- partial broker response;
- stale account snapshot;
- DB unavailable;
- process restart.

Success criteria are fail-closed behavior, bounded recovery and reconciliation.

## Reference engine boundary

Nautilus Trader and similar frameworks are research/reference sources only. Do not replace ASTRA's OMS/risk/accounting authority unless a separate migration project proves functional equivalence and all safety gates.

## Prohibited

Do not:

- enable live trading because engineering tests are green;
- treat backtest/MLflow rankings as proof of profitability;
- allow MLflow/OPA/CCXT to submit orders;
- bypass ASTRA adapters;
- hide fees/slippage from research evidence;
- merge engineering qualification with strategy promotion;
- weaken HALT/ARM for convenience.

## Suggested issue order

1. ASTRA-INT-00 Replay evidence hardening.
2. ASTRA-INT-01 Adapter conformance.
3. ASTRA-INT-02 Hypothesis invariants.
4. ASTRA-INT-03 CrossHair critical functions.
5. ASTRA-INT-04 Mutation tests.
6. ASTRA-INT-05 Semgrep architecture rules.
7. ASTRA-INT-06 Experiment registry.
8. ASTRA-INT-07 OPA qualification policy.
9. ASTRA-INT-08 in-toto attestations.
10. ASTRA-INT-09 Fault qualification.

## Definition of complete

The roadmap is complete only when each adopted assurance layer is part of the normal qualification pipeline, creates reproducible evidence and cannot bypass trading authority.

**Implementation instruction:** strengthen correctness and evidence. Do not interpret this roadmap as permission to take financial risk.

## Additional wave — signed artefacts and production observability

### Sigstore Cosign artefact signing — ADOPT/CI

Reference: https://github.com/sigstore/cosign

Complement the planned in-toto provenance chain with cryptographic signing of the built deployment artefact/image.

Chain:

`source SHA -> qualification evidence -> build -> SBOM/attestation -> Cosign signature -> admitted deployment digest`

The deployed runtime must be traceable to an immutable digest, not merely a mutable tag.

This strengthens supply-chain integrity only. A signed build is **not** evidence that the strategy is profitable or authorised for live trading.

### OpenTelemetry runtime tracing — ADOPT

References:

- https://github.com/open-telemetry/opentelemetry-python
- https://github.com/open-telemetry/opentelemetry-collector-contrib

Instrument low-cardinality operational traces around:

`market-data receive -> normalization -> strategy target -> risk decision -> OMS intent -> adapter request -> broker response -> reconciliation`

Do not export raw secrets, full market streams, strategy proprietary payloads or account PII in traces.

Trace context must never become part of deterministic trading/accounting inputs.

### Prometheus + Grafana operational contour — ADOPT

References:

- https://github.com/prometheus/prometheus
- https://github.com/grafana/grafana

Expose **operational**, not strategy-promotional, metrics:

- market-data freshness/gap count;
- adapter latency/error rates;
- order-intent queue depth;
- unresolved ambiguity count;
- reconciliation age;
- HALT/READ_ONLY state;
- DB/worker health;
- clock/skew alarms;
- restart/recovery count.

Strategy PnL/edge research may have separate dashboards, but production operational dashboards must not hide safety state behind performance charts.

Alert examples:

- stale market data;
- reconciliation overdue;
- unknown/unresolved broker state;
- authoritative DB unavailable;
- unexpected live/mainnet route;
- HALT bypass attempt.

### Acceptance extension

- release artefact signature verifies before admission;
- observability does not alter deterministic business inputs;
- metrics expose safety/reconciliation state;
- operational alert tests include fault-injection scenarios;
- none of these integrations change the live-trading authorization gate.

**Sequencing:** metrics/tracing can be developed before live admission; Cosign follows deterministic build/in-toto/SBOM generation.

## Additional wave — formal state-machine verification, deterministic time and shadow execution

This wave strengthens ASTRA's most safety-critical boundaries without adding strategy authority or live permission.

### Formal OMS / Risk State Model — ADOPT

References:

- https://github.com/tlaplus/tlaplus
- https://github.com/apalache-mc/apalache

Model a deliberately small abstract state machine covering critical invariants such as:

- trading mode / ARM / HALT;
- order intent lifecycle;
- submit-started ambiguity;
- broker acknowledgement;
- partial fill / fill / cancel / reject;
- pending exposure reservation;
- portfolio/accounting application;
- recovery/reconciliation;
- restart with unresolved broker state.

The formal model is not production code. It is an executable specification used to search for invalid interleavings and counterexamples.

Core invariants should include:

- no order submission when execution is not authorised;
- no duplicate accounting of the same fill;
- pending + realised exposure cannot bypass configured limits;
- unresolved submit ambiguity cannot create a second blind submit;
- HALT/READ_ONLY cannot be bypassed by restart;
- terminal order states cannot transition back to active without an explicit new intent;
- recovery converges to one authoritative broker/accounting state or remains fail-closed.

Apalache/model-checking results become qualification evidence tied to the specification version.

### Deterministic Virtual Clock Boundary — ADOPT

Replace direct wall-clock reads inside deterministic strategy/risk/accounting logic with an injected clock/time source where technically feasible.

Use cases:

- stale-data checks;
- order timeout/ambiguity windows;
- session boundaries;
- cooldowns;
- funding/mark intervals;
- replay timing;
- recovery deadlines.

Tests can then advance time explicitly without sleeping.

Wall-clock time still exists at adapters/IO boundaries, but the authoritative decision function receives a timestamp/clock context.

This makes replay, property testing and incident reproduction substantially more reliable.

### Shadow Execution Comparator — ADOPT

Add a no-submit shadow contour that can consume the same market/account snapshots and produce expected intents/risk decisions without touching broker execution.

Compare:

production/paper authoritative decision -> shadow decision -> structured diff

Diff dimensions:

- target position;
- order intent;
- risk allow/deny + reason;
- size/price rounding;
- expected pending exposure;
- expected account mutation after a supplied broker event.

Use cases:

- validating a new strategy/risk version against the current one;
- verifying migration/refactor equivalence;
- replaying a production incident with candidate fixes;
- detecting behaviour drift before promotion.

Shadow mode must be technically incapable of reaching the broker submit path.

### Walk-forward Strategy Evidence Protocol — ADOPT

Formalise promotion evidence for strategy research without predicting future profitability.

Every strategy candidate should declare:

- research hypothesis;
- dataset/replay version;
- training/tuning interval where applicable;
- untouched validation/test intervals;
- walk-forward windows;
- fee/funding/slippage assumptions;
- parameter-selection rule;
- baseline strategy;
- rejection thresholds;
- stability/sensitivity results.

Do not repeatedly tune on the same supposed holdout and continue calling it out-of-sample.

The protocol records evidence quality; it does not guarantee future returns and does not change live-trading authorization.

### State-transition Coverage Matrix — ADOPT

Map:

formal transition -> production state transition -> unit/property/replay/fault test -> evidence artifact

This identifies authority transitions that exist in code but are absent from formal/replay qualification, or vice versa.

The matrix should become part of release qualification for risk/OMS changes.

### Additional acceptance

- a critical OMS/risk model has machine-checked invariants and retained counterexample evidence when failures are found;
- deterministic tests can advance time without real sleeps for core logic;
- shadow contour cannot call submit even under misconfiguration;
- strategy promotion evidence identifies exact untouched/walk-forward windows and assumptions;
- formal/replay/production transition coverage can be audited;
- none of these capabilities changes PROFITABILITY_NOT_PROVEN or live/mainnet gates.

**Sequencing:** existing replay + property tests -> deterministic clock refactor -> formal model -> transition coverage -> shadow comparator -> walk-forward promotion protocol integration.

## Additional wave — clock integrity admission and exchange-time evidence

This wave strengthens all timeout, staleness, replay and reconciliation decisions by explicitly proving that the runtime clock is trustworthy.

### Host time synchronisation contour — ADOPT/OPS

Primary reference: https://github.com/mlichvar/chrony

Optional low-latency/PTP reference where infrastructure justifies it: https://github.com/richardcochran/linuxptp

Use Chrony or an equivalent host-level time daemon as infrastructure, not as an application library.

Record operational signals such as:

- synchronization state;
- current estimated system offset;
- root dispersion/error estimate;
- selected source;
- last successful synchronization;
- stratum/source class where relevant.

If the runtime is deployed on a managed platform where direct daemon control is unavailable, expose the platform/time-source assumption explicitly and measure application-visible skew instead of pretending the daemon is under ASTRA control.

### Clock Integrity Admission Gate — ADOPT

Before enabling a trading-capable runtime contour, evaluate a bounded clock-health policy.

Inputs may include:

- host time-sync status;
- estimated offset/error;
- time since last successful sync;
- exchange/server timestamp comparison;
- monotonic-clock continuity;
- configured maximum skew.

Policy outcomes:

- HEALTHY;
- DEGRADED / READ_ONLY;
- HALT / NO_NEW_ENTRY.

Exact thresholds belong in versioned configuration and must be justified per venue/strategy/runtime.

A failed clock gate must never silently fall back to wall-clock assumptions.

### Wall-clock vs monotonic time separation — ADOPT

Use wall-clock UTC for:

- exchange/event timestamps;
- persisted audit timestamps;
- calendar/session boundaries;
- external reconciliation evidence.

Use monotonic time for:

- elapsed timeout measurement;
- retry/cooldown duration;
- heartbeat age;
- local latency measurement.

Do not calculate elapsed durations from wall-clock values where a clock correction could move time backward/forward.

This complements the planned injected virtual clock for deterministic tests.

### Exchange Timestamp Skew Monitor — ADOPT

For market/account/order events that carry provider/exchange timestamps, record privacy-safe operational skew samples:

receive_wall_time - provider_event_time

Track by:

- provider;
- connection/feed type;
- event class;
- runtime node;
- release SHA.

Use robust summaries rather than interpreting every network-latency sample as host-clock error.

Large or structurally changed skew should create an operational alarm and may trip the clock admission policy depending on configuration.

### Time Evidence Snapshot — ADOPT

At start/recovery/incident boundaries, persist a compact evidence record:

- runtime/build SHA;
- system UTC;
- monotonic baseline/reference;
- sync source/status;
- estimated offset/error;
- provider server-time sample where available;
- gate result;
- config/policy version.

This becomes part of incident/replay qualification so later analysis knows whether clock integrity was healthy.

### Fault tests — ADOPT

Add qualification scenarios for:

- simulated wall-clock jump forward;
- simulated wall-clock jump backward;
- loss of time sync;
- excessive provider/server skew;
- monotonic timer continuity;
- stale-data gate around clock anomalies.

Core risk/OMS tests must prove that clock anomalies cannot create duplicate submits, stale-data acceptance or premature timeout recovery.

### Licensing/deployment boundary

Chrony and linuxptp upstream repositories are GPL-2.0. Treat them as host/infrastructure daemons or operational references; do not vendor/link them into ASTRA application code without a separate licensing review.

### Additional acceptance

- elapsed-time logic uses monotonic time where appropriate;
- time-sync health is visible and versioned in release/incident evidence;
- unhealthy clock state can force READ_ONLY/HALT according to policy;
- provider timestamp skew is monitored without confusing latency with certainty of host drift;
- replay/tests can reproduce clock-anomaly behavior;
- this gate does not change PROFITABILITY_NOT_PROVEN or live/mainnet authorization.

**Sequencing:** deterministic clock boundary -> host/time observability -> clock admission policy -> provider skew monitoring -> fault qualification -> release evidence.

## Additional wave — tail-latency evidence and coordinated omission control

This wave strengthens production qualification by measuring latency as a distribution with explicit semantics rather than relying on averages.

### HdrHistogram latency recorder — ADOPT

Reference:

https://github.com/HdrHistogram/HdrHistogram_py

Use HdrHistogram or an equivalent bounded recorder for selected operational latency paths.

Candidate measurements:

- market message receive -> normalized event;
- normalized event -> strategy target;
- target -> risk decision;
- risk decision -> OMS intent committed;
- OMS submit start -> broker acknowledgement/response;
- broker event receive -> accounting/reconciliation applied;
- reconciliation request -> authoritative result;
- end-to-end paper/shadow decision paths.

Each histogram must define:

- start/end event;
- clock source;
- unit;
- lowest/highest trackable value;
- significant digits;
- aggregation window;
- runtime/release;
- provider/adapter where applicable.

### Percentile / Tail Metrics — ADOPT

Report at least where meaningful:

- count;
- min/max;
- p50;
- p90/p95;
- p99;
- p99.9 for sufficiently large samples.

Averages may remain available but must not be the primary operational qualification metric.

### Processing vs Provider / Network Latency — ADOPT

Where timestamps permit, separate:

- local processing time;
- queue/wait time;
- network/provider round-trip;
- provider event-to-receive delay;
- recovery/reconciliation age.

Do not present exchange/provider timestamps as perfectly synchronized unless Clock Integrity evidence supports the assumption.

### Coordinated Omission Control — ADOPT

Load/latency tests must explicitly document whether they account for coordinated omission.

If the system stalls and the test generator also stops generating expected work, naive latency measurements can hide the stall.

Qualification tests should therefore include a schedule/expected-arrival model where appropriate and record corrected/uncorrected histograms separately.

### Latency Budget Authority — ADOPT

Create versioned operational budgets per path/environment:

- expected/target threshold;
- hard safety threshold where relevant;
- percentile;
- evaluation window;
- action on breach.

Examples:

- warning;
- degraded/read-only;
- fail qualification;
- investigate only.

Do not convert a performance budget into a live-trading permission.

### Replay / Fault Correlation — ADOPT

Fault campaigns should retain latency distributions before/during/after:

- network delay;
- broker/API slowdown;
- DB contention;
- process restart;
- reconciliation backlog;
- market-data burst.

This allows release evidence to answer not only "did it recover?" but "how degraded did it become and did safety state react?".

### Additional acceptance

- latency reports declare exact measurement boundaries and clock;
- tail percentiles are retained for qualification, not only averages;
- coordinated-omission behavior is documented/tested for load qualification;
- provider/network and local processing are not conflated when separable;
- budget breaches cannot be hidden by aggregation;
- none of these metrics enable mainnet/live routing or change PROFITABILITY_NOT_PROVEN.

**Sequencing:** deterministic/monotonic clock + OpenTelemetry metrics foundation -> HdrHistogram recorders -> budgets -> fault/replay correlation -> release qualification evidence.

**Dependency note:** HdrHistogram Python is currently Apache-2.0 upstream; keep instrumentation outside deterministic decision inputs and benchmark recording overhead before enabling high-frequency paths.

## Premium innovation wave — market microstructure stress lab and protocol conformance

This wave adds a controlled synthetic-market qualification layer for ASTRA. It strengthens robustness/evidence; it does not prove profitability and cannot authorise live trading.

### Market Microstructure Scenario Lab — ADOPT/RESEARCH SIDECAR

Research candidate:

https://github.com/abides-sim/abides

Use an agent-based/synthetic exchange simulator only in an isolated research/qualification environment.

Scenario families may include:

- thin/deep order book;
- spread widening/narrowing;
- volatility bursts;
- fragmented liquidity;
- delayed acknowledgements;
- partial fills;
- queue-position uncertainty;
- trading halt/resume;
- auction/open/close transitions;
- stale or gapped market data;
- rapid cancel/replace pressure;
- provider latency burst.

The purpose is to test ASTRA reactions and invariants under difficult market mechanics, not to model manipulative trading tactics for deployment.

### Synthetic Venue Adapter — ADOPT

Implement the simulator through the same provider/adapter boundary used by paper/replay qualification where feasible.

Flow:

scenario definition -> synthetic venue events -> normalizer -> strategy target -> risk -> OMS -> synthetic broker/exchange response -> accounting/reconciliation

This maximises reuse of production code paths.

The simulator must be technically isolated from real broker credentials and live submit routes.

### Scenario Corpus Authority — ADOPT

Each qualification scenario stores:

- scenario ID/version;
- random seed;
- simulator/provider version;
- instrument specification;
- initial book/state;
- event schedule/distribution parameters;
- latency/fault parameters;
- expected invariants;
- result/evidence artifact;
- release SHA.

A scenario result is reproducible evidence, not a market forecast.

### Differential Replay vs Simulation Qualification — ADOPT

Use three complementary evidence sources:

1. historical exchange replay;
2. synthetic microstructure scenarios;
3. explicit fault injection.

Compare:

- risk allow/deny behavior;
- OMS transitions;
- exposure reservation;
- duplicate-event handling;
- timeout/recovery;
- reconciliation convergence;
- latency-tail behavior.

No one source replaces the others.

### FIX Protocol Conformance — CONDITIONAL

Reference implementation:

https://github.com/quickfix/quickfix

Only if a target broker/venue actually requires FIX, add protocol-level conformance fixtures for:

- session logon/logout;
- sequence numbers;
- resend/gap-fill;
- heartbeat/test request;
- order identifiers;
- execution reports;
- cancel/replace/reject;
- reconnect/recovery.

QuickFIX is a protocol/reference implementation candidate; ASTRA remains the order/risk/accounting authority.

Do not adopt FIX infrastructure until a real provider contract requires it.

### Market-State Safety Matrix — ADOPT

Map scenario states to expected ASTRA safety behavior:

- normal;
- delayed;
- stale;
- disconnected;
- ambiguous submit;
- partial account truth;
- halted venue;
- recovering;
- unreconciled.

For each state define:

- allowed actions;
- prohibited actions;
- expected ARM/HALT/read-only result;
- required evidence before recovery.

### Qualification Dashboard — ADOPT

Present evidence by release:

- scenarios passed/failed;
- counterexample/failed invariant;
- historical replay status;
- synthetic stress status;
- clock integrity;
- tail latency;
- reconciliation;
- unresolved gaps.

This is engineering qualification, not a trading-performance dashboard.

### Additional acceptance

- simulator environment has no live broker credentials;
- synthetic venue uses the same normalisation/risk/OMS path where feasible;
- scenario corpus is seed/version reproducible;
- scenario success does not change PROFITABILITY_NOT_PROVEN;
- protocol simulator/reference cannot bypass dispatch authority;
- recovery remains fail-closed on unresolved venue/account state;
- ABIDES/QuickFIX licensing and exact versions are reviewed before operational use.

**Sequencing:** replay + deterministic clock + state model -> synthetic adapter -> scenario corpus -> differential qualification -> optional FIX conformance when provider need exists -> qualification dashboard.

**Commercial framing:** position this as institutional-grade market-behaviour qualification and evidence, not as a profit predictor or autonomous live-trading permission.

## Premium commercial wave — Market Data Integrity and Reference Price Authority

This wave turns market data quality into a first-class qualification product. It does not add strategy logic or permission to trade live.

### Market Data Integrity Authority — ADOPT

For every provider/feed/instrument, maintain a bounded integrity state derived from observed evidence:

- connection/feed identity;
- instrument;
- sequence continuity;
- duplicate/out-of-order count;
- timestamp sanity;
- stale-age;
- crossed/locked/invalid book markers where applicable;
- impossible price/size values;
- update-rate collapse/spike;
- provider heartbeat/health;
- normalization errors;
- reconciliation state.

Each integrity decision stores:

- rule/version;
- source feed/provider;
- affected interval;
- evidence counters/samples;
- severity;
- resulting action.

### Feed Quality Gate — ADOPT

Define explicit outcomes:

- HEALTHY;
- DEGRADED;
- QUARANTINED;
- STALE / READ_ONLY;
- HALT FOR AFFECTED INSTRUMENT/FEED.

Bad data must fail closed before it reaches strategy/risk as trusted market state.

A recovery requires fresh continuity/health evidence according to a versioned rule; reconnect alone is not sufficient.

### Cross-Feed / Cross-Venue Divergence Monitor — ADOPT

Where multiple legitimate reference feeds exist, compare normalized observations to detect:

- abnormal price divergence;
- stale primary feed;
- spread/quote regime inconsistency;
- provider-specific jumps;
- timestamp/sequence anomalies.

A second venue/feed is not automatically "truth". The system records disagreement and may downgrade confidence or select an explicitly configured reference hierarchy.

### Reference Price Confidence — ADOPT

Create a transparent reference-price state for risk/marking use cases where required.

Inputs may include:

- selected provider/venue;
- freshest valid bid/ask/trade;
- multiple-feed agreement;
- stale threshold;
- market state;
- outlier policy.

Persist:

- price/value;
- source set;
- timestamp;
- method/rule version;
- confidence/status;
- excluded sources/reasons.

Do not hide a fallback source switch.

### Tick / Market-data Evidence Archive — ADOPT/ADAPT

For qualification and incident reconstruction, persist a bounded immutable market-data evidence stream or snapshots with:

- raw/provider event identity where retained;
- normalized event;
- receive/exchange timestamp;
- sequence;
- instrument-spec version;
- feed/provider;
- release SHA;
- checksum/partition ID.

Apache Arrow / Parquet-style columnar formats may be used for portable offline evidence and analytics:

https://github.com/apache/arrow

This archive is replay/qualification evidence, not the live decision store.

### High-volume Time-series Analytical Sidecar — CONDITIONAL

Reference:

https://github.com/questdb/questdb

QuestDB may be evaluated as a read-only/analytical sidecar if tick/latency/quality volume makes PostgreSQL/reporting inconvenient.

Allowed uses:

- feed-health timelines;
- latency/skew analytics;
- divergence analysis;
- incident exploration;
- qualification dashboards.

It must not become order, portfolio, risk or trading authority.

### Data Quality Incident Dossier — ADOPT

For any material integrity event, generate a reproducible dossier:

- affected feed/instruments;
- start/end;
- integrity rules triggered;
- raw/normalized evidence refs;
- clock state;
- tail-latency state;
- provider divergence;
- system safety response;
- recovery evidence;
- release SHA.

This gives an institutional-grade explanation of why ASTRA trusted, degraded or rejected a feed.

### Additional acceptance

- corrupt/stale/gapped market data cannot silently become trusted market state;
- every failover/reference-price change is visible and versioned;
- divergence does not auto-declare one venue "correct" without configured policy;
- evidence archive can reproduce a market-data integrity incident;
- analytical sidecar is replaceable and cannot submit orders or mutate portfolio/risk;
- this wave does not alter PROFITABILITY_NOT_PROVEN, mainnet or live-trading authorization.

**Sequencing:** adapter conformance + replay + clock integrity -> feed integrity rules -> cross-feed divergence -> reference-price confidence -> evidence archive -> optional time-series sidecar -> incident dossier.

**Commercial framing:** position this as institutional-grade market-data assurance and explainable feed trust, not as an alpha or prediction feature.

## Premium enterprise wave — Transaction Cost Analysis and execution quality authority

This wave turns ASTRA execution evidence into an institutional-grade post-trade quality layer. It does not add trading strategy logic and cannot authorise live trading.

### Execution Benchmark Authority — ADOPT

For every qualified/paper/external-readonly execution observation, define reproducible benchmarks such as:

- decision/arrival mid;
- decision-side best bid/ask;
- submitted price;
- broker acknowledgement time;
- first fill;
- volume-weighted fill price;
- completion price/time;
- post-trade mark at configured horizons;
- fees/commission/funding where applicable.

Every benchmark stores source feed, timestamp, clock-health state, instrument-spec version and benchmark-rule version.

### Implementation Shortfall — ADOPT

Calculate an explicit decomposition from decision price to realised execution outcome.

Possible components:

- spread crossing;
- price drift while waiting;
- fill fragmentation;
- explicit fees;
- residual/unfilled opportunity;
- cancel/replace effect;
- provider/venue latency context.

Do not present this as trader skill or alpha. It is execution-quality evidence.

### Slippage / Fill Quality — ADOPT

Metrics may include:

- signed slippage vs arrival/reference;
- fill ratio;
- time to first fill;
- time to complete;
- average/percentile execution delay;
- effective spread;
- realised spread where methodology is applicable;
- reject/cancel rate.

Methodology, sample size and exclusions must always be visible.

### Post-trade Adverse-selection Signal — ADOPT

For completed fills, compare subsequent market movement at configured time/event horizons.

This may indicate whether fills systematically occur immediately before adverse price movement.

It is an analytical diagnostic only. It must not automatically alter strategy/routing without a separately reviewed research/promotion process.

### Venue / Adapter / Order-type Comparison — ADOPT

Where observations are comparable, analyze execution quality by:

- provider/venue;
- instrument;
- order type;
- strategy version;
- liquidity regime;
- size bucket;
- time/session.

Do not rank providers from tiny/non-comparable samples.

### TCA Evidence Dataset — ADOPT

Persist a reproducible analytical record:

- intent/order/fill IDs;
- decision snapshot;
- market-data evidence references;
- execution events;
- benchmark values;
- method/config version;
- fees;
- TCA metrics;
- release SHA.

This dataset is derived/read-only. OMS/accounting remain authority.

### Execution Quality Dashboard — ADOPT

Show:

- implementation shortfall distribution;
- slippage percentiles;
- fill rates;
- time-to-fill;
- venue/provider comparison;
- rejected/cancelled orders;
- quality by market regime;
- unresolved data-quality flags.

Every chart resolves back to execution/market-data evidence.

### Safety / Qualification Boundary — REQUIRED

TCA findings may create a research/engineering review item.

They cannot:

- change routing automatically;
- enable mainnet/live;
- promote a strategy;
- bypass risk;
- reinterpret PROFITABILITY_NOT_PROVEN.

### Additional acceptance

- every benchmark is reproducible from source events and rule version;
- clock/data-integrity state is retained with execution metrics;
- fees and missing/unfilled quantities are not silently omitted;
- provider comparisons expose denominator/sample conditions;
- TCA dataset cannot mutate order/accounting state;
- dashboards clearly separate execution quality from strategy profitability.

**Sequencing:** market-data integrity + clock integrity + OMS/accounting evidence -> TCA dataset -> benchmark calculations -> post-trade diagnostics -> execution-quality dashboard.

**Commercial framing:** position ASTRA as an explainable institutional execution and qualification platform, not merely an order-submission bot.

## Moat wave — Best Execution Governance and regulatory evidence packs

This wave turns ASTRA's TCA, market-data integrity, clock integrity, OMS and reconciliation evidence into a governed execution-policy product for institutional use.

It does not itself make ASTRA legally compliant in any jurisdiction and does not authorise live trading.

### Execution Policy Authority — ADOPT

Create a versioned policy object describing, per account/provider/instrument class where applicable:

- eligible venues/providers;
- routing/adapter eligibility;
- supported order types;
- data-quality prerequisites;
- stale/clock thresholds;
- price/reference hierarchy;
- liquidity/size constraints;
- failover rules;
- prohibited states;
- evidence required before recovery;
- effective dates;
- owner/approver.

Historic policy versions are immutable.

### Decision / Route Rationale Record — ADOPT

For each submitted or simulated qualified order, retain enough evidence to explain:

- strategy/intent ID;
- risk result;
- selected provider/venue/adapter;
- available eligible alternatives known to the system;
- market/reference-price state;
- market-data integrity state;
- clock state;
- order type/size;
- policy version;
- route/dispatch reason code;
- resulting order/fill IDs.

Do not generate a retrospective rationale from current policy after the fact.

### Best-execution Evidence Pack — ADOPT

Generate a reproducible pack for a selected period/order/account containing:

- execution-policy version;
- market-data evidence;
- TCA metrics;
- rejected/failed alternatives where observable;
- order/fill/reconciliation history;
- fees/costs where available;
- provider incidents;
- clock/data-integrity state;
- overrides/exceptions;
- release SHA.

This is an engineering/evidence artefact. Jurisdiction-specific legal reports require separate legal/compliance review.

### Policy Exception Workflow — ADOPT

Any manual or configured exception records:

- rule being overridden;
- scope;
- reason;
- actor/approver;
- start/end;
- affected orders/providers;
- follow-up/review state.

No undocumented bypass.

### Periodic Execution Review — ADOPT

Use TCA and provider-quality evidence to review:

- fill quality;
- implementation shortfall;
- reject/cancel rates;
- provider latency;
- market-data incidents;
- routing exceptions;
- reconciliation issues;
- sample sufficiency.

The review can propose policy changes.

It cannot update execution policy automatically.

### Jurisdiction / Client Rule Packs — CONDITIONAL

Create separate reviewed mappings when a real institutional client/jurisdiction requires them.

For example, European best-execution obligations under MiFID II should be mapped against the then-current legal/regulatory requirements and client scope, not hard-coded from an old template.

Reference context:

https://www.esma.europa.eu/document/review-mifid-ii-framework-best-execution-reports-investment-firms

Store:

- jurisdiction;
- regulation/policy reference;
- applicability;
- required fields/evidence;
- legal/compliance reviewer;
- effective date;
- implementation mapping.

### Governance Dashboard — ADOPT

Show:

- policy versions;
- current exceptions;
- evidence-pack completeness;
- provider execution quality;
- data/clock qualification;
- orders lacking required evidence;
- policy-review actions.

This is a governance surface, not a profitability screen.

### Additional acceptance

- every route/submit resolves to the exact policy version in force;
- rationale is based on contemporaneous facts, not reconstructed current state;
- overrides are explicit/audited;
- evidence pack reproduces from canonical market/order/TCA records;
- legal/regulatory rule packs are separately reviewed and versioned;
- no compliance badge/claim is generated automatically;
- this wave does not enable live/mainnet or change PROFITABILITY_NOT_PROVEN.

**Sequencing:** OMS/Risk/Reconciliation + Market Data Integrity + TCA -> Execution Policy -> route rationale -> exception workflow -> evidence pack -> periodic governance review -> jurisdiction-specific mappings.

**Commercial framing:** this opens institutional execution-governance and audit/evidence use cases, creating a high-trust moat around ASTRA's deterministic architecture rather than competing on strategy claims.

## Platform economics wave — Qualification API and Adapter Certification

This wave exposes ASTRA's strongest asset — deterministic qualification evidence — as a controlled platform service for broker adapters, institutional teams and execution infrastructure vendors.

It does not expose a public trading API and cannot enable live trading.

### Qualification Job Authority — ADOPT

Create a bounded job model:

- job ID;
- submitter/organisation;
- target adapter/build/artifact;
- qualification profile/version;
- market-data fixture/replay/scenario set;
- expected capabilities;
- started/completed time;
- result;
- evidence package;
- release SHA;
- status.

A qualification job never receives live broker credentials unless a separately authorised controlled environment explicitly requires them.

### Adapter Certification Contract — ADOPT

Define machine-testable profiles for adapters:

- symbol/instrument normalisation;
- price/quantity precision;
- order-type support;
- idempotent client/order IDs;
- partial fill handling;
- cancel/replace;
- reject mapping;
- reconnect/recovery;
- sequence/order events;
- clock/timestamp handling;
- reconciliation;
- fail-closed submit ambiguity.

Profiles are versioned and venue/provider-specific where needed.

### Qualification API — ADOPT

Contract-first endpoints may support:

- submit qualification job;
- upload/reference adapter package/test build;
- select approved replay/scenario corpus;
- read job status;
- fetch machine-readable result;
- fetch evidence pack;
- validate certification status.

Do not expose arbitrary code execution without sandboxing and organisation authorization.

### Partner Sandbox — ADOPT

Provide a non-live environment with:

- simulated venue;
- historical replay;
- synthetic microstructure scenarios;
- deterministic clock;
- fault injection;
- test account state;
- no live submit credentials.

This is where external/in-house adapters are integrated before production qualification.

### Certification Result — ADOPT

Internal result states may include:

- contract validated;
- replay qualified;
- fault qualified;
- recovery qualified;
- data-integrity qualified;
- performance qualified;
- rejected / incomplete.

A result must identify exact:

- adapter version;
- test corpus/version;
- ASTRA release;
- policy/config;
- evidence checksum.

Do not call it regulatory certification.

### Continuous Requalification — ADOPT

Trigger requalification when:

- adapter version changes;
- exchange/provider API changes;
- instrument model changes materially;
- risk/OMS contract changes;
- replay/scenario corpus adds a critical case;
- policy version changes.

Historic results remain immutable.

### Evidence Verification Endpoint — ADOPT

An institution may verify:

- qualification job ID;
- result/status;
- version;
- evidence checksum;
- signed metadata where configured.

This allows proof of engineering qualification without exposing proprietary strategy logic.

### Usage Metering / Commercial Model — ADAPT

Potential units:

- qualification run;
- scenario pack;
- adapter;
- environment;
- retained evidence storage;
- enterprise support.

Reference for metering:

https://github.com/openmeterio/openmeter

Metering stays outside order/risk truth.

### Additional acceptance

- API cannot route a live order;
- sandbox has no live credentials;
- every result binds to exact adapter/corpus/config versions;
- certification status is revoked/superseded explicitly on breaking changes;
- evidence endpoint cannot expose proprietary market/account data outside scope;
- qualification remains independent from PROFITABILITY_NOT_PROVEN and live authorization.

**Sequencing:** Adapter Conformance + Replay + Fault Lab + Market Data/TCA/Best Execution evidence -> Qualification Job -> Partner Sandbox -> API -> Certification Registry -> continuous requalification -> metering.

**Commercial framing:** ASTRA becomes a high-trust execution-infrastructure qualification service that institutions and broker integrations can build around, not merely an internal trading runtime.

## Defensibility wave — ASTRA Qualification Standard and portable adapter credentials

This wave formalizes ASTRA's replay, fault, clock, market-data, TCA and best-execution evidence into a proprietary qualification standard that external integrations can satisfy and verify.

It does not certify profitability, regulatory compliance or permission to trade live.

### ASTRA Qualification Standard — ADOPT

Define a public/versioned standard with machine-testable profiles such as:

- Adapter Contract;
- Market Data Integrity;
- OMS State-Machine;
- Reconciliation/Recovery;
- Clock Integrity;
- Fault Tolerance;
- Tail Latency;
- Historical Replay;
- Synthetic Microstructure;
- Execution Evidence;
- Best-Execution Evidence.

Each profile declares:

- required inputs;
- test corpus;
- invariants;
- pass/fail conditions;
- evidence outputs;
- tool/config versions;
- requalification triggers.

### Qualification Manifest — ADOPT

Every qualified build/adapter produces a manifest containing:

- subject adapter/component;
- subject version/hash;
- ASTRA release;
- qualification standard/profile version;
- test corpus versions;
- environment;
- result by profile;
- evidence checksums;
- issued_at;
- expiry/requalification condition;
- status.

Historical manifests remain immutable.

### Portable Adapter Credential — ADAPT

Reference:

https://github.com/w3c/vc-data-model

Issue scoped credentials such as:

- ASTRA Adapter Contract Qualified;
- Market Data Integrity Qualified;
- Recovery Qualified;
- Replay Qualified.

Credential proves only that exact version passed that exact profile.

It does not mean the broker/venue endorses ASTRA and does not permit live/mainnet use.

### Build / Evidence Signature — ADAPT

Use Sigstore/Cosign-style artefact signing where appropriate:

https://github.com/sigstore/cosign

Bind:

adapter/build artifact -> qualification manifest -> evidence package -> signature identity

This helps detect substituted/unqualified binaries.

### Adapter / Provider Trust Graph — ADOPT

Graph:

provider/adapter -> versions -> qualification results -> incidents -> requalification -> compatibility -> deprecation

Useful dimensions:

- current qualification profile status;
- incident/open issue state;
- compatibility freshness;
- recovery-test recency;
- evidence completeness.

No opaque provider quality score.

### Public / Partner Verification Endpoint — ADOPT

Allow scoped verification of:

- credential/qualification ID;
- subject/version;
- standard/profile;
- result/status;
- issued/review/expiry;
- evidence hash.

Do not expose proprietary strategy/account data.

### Standard Governance — ADOPT

Every standard change requires:

- version bump;
- changelog;
- migration/requalification impact;
- owner/reviewer;
- effective date;
- deprecated profiles;
- minimum supported version.

Breaking changes cannot silently invalidate old evidence.

### Additional acceptance

- qualification profile is machine-testable;
- credential binds to exact component/version;
- signed evidence detects substitution;
- incidents/revocations remain visible;
- no qualification result alters PROFITABILITY_NOT_PROVEN;
- no credential implies regulatory certification/live authorisation;
- provider graph contains engineering evidence only.

**Sequencing:** Qualification API + Evidence Packs + Best Execution/TCA -> formal standard -> manifests -> signatures -> scoped credentials -> provider/version trust graph -> public verification.

**Moat:** ASTRA can become a de-facto engineering qualification standard for deterministic trading infrastructure, with accumulated versioned evidence and integration history that is difficult to reproduce.

## Institutional adoption wave — ASTRA Reference Qualification Network

This wave moves ASTRA from a proprietary qualification framework toward ecosystem adoption by institutions, broker integrations, infrastructure vendors and internal platform teams.

It does not create regulatory authority, profitability proof or live-trading permission.

### Public Reference Profiles — ADOPT

Publish bounded implementation-neutral reference profiles for:

- Adapter Contract;
- Market Data Integrity;
- OMS State Machine;
- Recovery/Reconciliation;
- Clock Integrity;
- Replay;
- Fault Tolerance;
- Execution Evidence.

Each reference profile should include:

- normative requirements;
- machine-readable schema;
- pass/fail examples;
- synthetic fixtures;
- expected evidence artefacts;
- version compatibility rules.

Reference material must not expose proprietary trading strategy logic.

### Synthetic Reference Implementation — ADOPT

Provide a non-live reference adapter / simulated venue path demonstrating:

`adapter -> replay -> fault scenario -> OMS -> reconciliation -> qualification manifest -> signed evidence -> verification`

Use synthetic credentials, synthetic account state and synthetic/private-safe market fixtures.

### Institutional Test Corpus — ADOPT

Curate a versioned scenario corpus for recurring integration failures:

- duplicate/late execution events;
- out-of-order private-stream updates;
- partial fills;
- ambiguous submit outcome;
- reconnect after lost ACK;
- stale/gapped market data;
- clock drift;
- venue reject mapping;
- cancel/replace race;
- reconciliation mismatch;
- sequence reset;
- degraded latency / timeout;
- recovery after process restart.

Every corpus version has changelog, severity and affected qualification profiles.

### Approved Adapter / Integration Network — ADOPT

Create scoped statuses for organisations/components such as:

- Adapter Contract Integrated;
- Replay Qualification Integrated;
- Recovery Qualification Integrated;
- Evidence Verification Integrated;
- Continuous Requalification Integrated.

A status proves only the tested integration/process scope.

### Institutional Evidence Registry — ADOPT

Maintain a registry of:

- qualification subject/version;
- standard/profile version;
- issued result;
- evidence hash;
- signature identity;
- active/superseded/revoked state;
- requalification trigger;
- incident/deprecation references.

Historical states remain queryable.

### Portable Verification Bundle — ADOPT

An institution should be able to verify offline or independently:

- subject build/adapter identity;
- standard/profile version;
- test-corpus versions;
- qualification result;
- evidence checksums;
- signature chain;
- registry/checkpoint status at issuance;
- supersession/revocation pointers.

Verification must not require access to proprietary strategy/account data.

### OEM / Embedded Qualification — CONDITIONAL

Allow broker technology vendors, OMS/EMS providers or institutional platforms to embed bounded ASTRA qualification workflows through:

- API;
- CLI/runner;
- CI integration;
- evidence verification SDK;
- private scenario packs.

Commercial embedding must never inherit live-routing authority.

### Enterprise Bundle — ADOPT

Potential product bundles:

- Adapter Qualification;
- Continuous Requalification;
- Replay & Fault Lab;
- Execution Evidence Archive;
- Best-Execution/TCA Evidence;
- Enterprise Verification API;
- Private Scenario Pack;
- Integration Support.

### External Contribution Model — CONDITIONAL

Approved partners may contribute:

- anonymised incident classes;
- synthetic reproductions;
- adapter compatibility notes;
- proposed conformance tests;
- non-confidential venue edge cases.

Contribution admission requires review, provenance and licensing.

No partner-contributed case becomes a normative profile silently.

### Consortium / Working-group Participation — CONDITIONAL

Where commercially useful, participate in industry work around:

- deterministic execution testing;
- electronic-trading interoperability;
- execution evidence;
- replay/fault qualification;
- time synchronisation and data integrity.

Do not claim external standard recognition until formally granted.

### Accumulated Evidence Switching Cost — ADOPT

Legitimate switching cost comes from:

- historical qualification manifests;
- incident-to-test-case lineage;
- adapter version history;
- requalification history;
- compatibility history;
- signed evidence archives;
- institutional integration mappings;
- private scenario corpora.

Avoid artificial lock-in: export and independent verification should remain possible.

### Additional acceptance

- all public/reference assets are non-live and strategy-safe;
- a partner status never implies regulatory approval;
- corpus additions are versioned and reviewable;
- verification works from explicit signed/evidence artefacts;
- revocation/supersession preserves history;
- institutional/OEM clients cannot turn qualification APIs into live order-routing authority;
- public reference material contains no secrets or customer trading data.

**Sequencing:** Qualification Standard -> reference profiles -> synthetic implementation -> institutional corpus -> evidence registry -> verification bundle -> approved integration network -> OEM/enterprise distribution.

**Moat:** ASTRA's defensibility compounds when institutions reuse the same deterministic profiles, test corpora, signed evidence format and requalification history across multiple adapters and providers.


## Qualification trust execution map — 2026-10-07

This snapshot records the qualification/trust layers that are already present in canonical `main`, the current in-flight slice and the next planned sequence. It is descriptive evidence, not live-trading authority.

### Canonical in `main`

Verified by merged repository history / file presence in current `main`:

- authenticated Evidence Registry;
- authenticated Qualification Profile Registry;
- combined Qualification Trust Checkpoint v3;
- Portable Qualification Verification Bundle v3;
- Qualification Verification Service v3;
- Profile Lifecycle Event Journal;
- Profile Lifecycle Transparency Publication Authority;
- incremental Profile Event Delta Proof;
- profile-history-aware Qualification Trust Checkpoint v4;
- Portable Qualification Verification v4 (#254);
- institutional adoption / mandatory master-plan governance (#255);
- stateful Qualification Verification Service v4 with TrustState transition output (#256);
- Portable Artifact Codec v1 with deterministic `ASTRA_CANONICAL_JSON_V1` representation (#257);
- master-plan typed-decoder / rollback-discipline gate (#258);
- safe typed Portable Verification Bundle v4 decoder (#260);
- independent Offline Qualification Verifier CLI v1 with local TrustState authority discipline (#262).

These layers collectively provide immutable qualification/profile bindings, signed evidence, lifecycle state, append-only lifecycle history, transparency publication, portable verification, deterministic artifact transport, strict typed reconstruction and independent offline verification. They do not prove profitability or enable live/mainnet trading.

### In-flight

- PR #267 — fresh rebuild of the `urllib3 2.8.0` hash-locked security remediation on current canonical `main`; supersedes #266.
- PR #264 — Persistent TrustState Authority v1.

Strict dependency order:

`#267 full PASS -> merge -> retarget/rebuild #264 on new main -> #264 qualification coverage/regression PASS -> merge`.

Only after those two layers are canonical may implementation move to the contract-first Verification API.

### Next sequence after verified merge

Current canonical `main` now includes:

1. Qualification Verification Service v4;
2. Portable Artifact Codec v1;
3. deterministic `ASTRA_CANONICAL_JSON_V1` representation;
4. persisted TrustState v4 bound into the portable artefact.

Before the offline CLI verifier, add one explicit prerequisite:

5. safe typed Portable Verification Bundle v4 decoder;
   - reconstruct only known qualification dataclasses from the already canonical, hash-validated JSON tree;
   - reject unknown/missing fields at every typed boundary;
   - reject arbitrary-object or pickle-style deserialisation;
   - preserve exact timestamps, integer domains, tuple/list semantics and signature bytes;
   - re-run the existing `bundle.validate()` after reconstruction;
   - prove `decoded typed bundle -> payload()` is byte-for-byte equal to the canonical embedded bundle payload;
   - fuzz/property-test malformed nested payloads and fail closed.

This prerequisite is **ADOPT** because the current Portable Artifact Codec deliberately returns the embedded bundle as an immutable JSON mapping while `QualificationVerificationServiceV4` deliberately accepts a typed `PortableQualificationVerificationBundleV4`. The decoder must remain a narrow validation boundary rather than hidden ad-hoc conversion inside a CLI/API.

Then continue:

6. offline CLI verifier;
7. CBOR representation only if canonical encoding rules and cross-implementation interoperability tests are explicit;
8. contract-first Verification API;
9. SDK contract;
10. reference profiles and institutional test corpus;
11. OEM/embedded qualification and enterprise verification integration;
12. continuous requalification and evidence-history export.

### Typed decoder acceptance evidence — ADOPT

The typed decoder slice is complete only when:

- every nested bundle object has an explicit schema-to-dataclass conversion path;
- canonical JSON digest and bundle identity are checked before typed reconstruction;
- reconstructed `payload()` exactly matches the embedded canonical bundle payload;
- malformed/unknown nested fields fail closed;
- no dynamic import, eval, pickle or arbitrary class instantiation is used;
- the decoder itself cannot call broker, OMS, risk or live-routing authority;
- the offline verifier can consume only the decoded typed bundle plus explicit trusted roots / persisted TrustState.

### Offline verifier local trust-anchor discipline — ADOPT

The offline verifier must not treat the portable artefact's embedded TrustState as sufficient local authority.

Required model:

`local trusted state -> artifact embedded state equality -> typed bundle verification -> next trusted state`

Rules:

- normal verification requires an independently persisted local TrustState input;
- the embedded TrustState is transport context and must exactly match local trusted state before verification proceeds;
- a mismatch fails closed before any state advancement;
- missing local state never silently implies genesis;
- genesis bootstrap requires an explicit operator flag;
- genesis bootstrap is allowed only when profile event count/head and Trust Checkpoint v4 SHA are at genesis and the transparency anchor exactly matches the root-anchored base v3 transparency head;
- REJECTED verification never advances TrustState;
- VERIFIED verification may advance TrustState even when the qualification result is currently unusable, so authenticated revocation/supersession history cannot be ignored;
- state persistence uses temp-write + flush/fsync where supported + atomic replace, preserving the previous state on every error;
- rollback to an older otherwise valid artefact/state pair is treated as a trust-anchor violation, not as a valid replay.

Acceptance evidence must include:

- local-state mismatch rejection;
- explicit genesis-bootstrap tests;
- wrong trusted-root rejection;
- byte-for-byte state preservation after REJECTED/error outcomes;
- deterministic state-file encoding;
- successful advancement on authenticated lifecycle changes, including unusable/revoked outcomes.

This is an **ADOPT** requirement for the offline CLI, Verification API and SDK contract. It strengthens independent verification only and does not create trading authority.

### Persistent TrustState Authority v1 — ADOPT

After the offline CLI is qualified, replace ad-hoc single-file state advancement with a bounded local authority.

State record:

- authority/schema version;
- monotonically increasing generation;
- previous record SHA-256;
- current TrustState v4 payload and state SHA-256;
- source artifact ID/SHA;
- verified bundle/checkpoint identity where available;
- verification timestamp;
- record SHA-256 over canonical JSON.

Storage model:

- one immutable canonical history record per generation;
- one canonical current pointer/record;
- history record is durably committed before current advances;
- temporary/partial files are never authoritative;
- all writes use flush/fsync where supported plus atomic replace;
- current generation advances only under an exclusive process lock.

CAS discipline:

- caller supplies expected generation, expected current record SHA and expected TrustState SHA;
- authority reloads current state while holding the lock;
- any mismatch rejects the update without mutation;
- generation increments exactly by one;
- TrustState event/tree counters cannot regress;
- duplicate/no-op advancement is rejected unless an explicit idempotent replay contract is later defined.

Crash recovery:

- on open, validate every relevant canonical record before trusting it;
- verify generation continuity and previous-record hash chain;
- if current points to a valid history record, it is authoritative;
- if exactly one fully committed next history record exists after a crash-before-current-update, recovery may deterministically advance current to it;
- ambiguous forks, gaps, multiple competing next records, malformed records or hash mismatches fail closed and require operator recovery;
- orphan temporary files are ignored/cleaned only after the authoritative chain is established.

Rollback semantics:

- local hash-chain/history makes ordinary rollback detectable when newer history remains;
- it does **not** make rollback impossible against an attacker able to replace the entire local authority directory and all external anchors;
- stronger anti-rollback requires an external monotonic/checkpoint anchor, remote witness, TPM/HSM counter or equivalent separately qualified mechanism;
- documentation and product claims must say tamper-evident / rollback-detecting within the retained authority boundary, not tamper-proof.

Receipts:

- every accepted advancement yields an exportable canonical receipt bound to previous/current record SHA, generation, state SHA and verification artifact/checkpoint;
- receipt signing is optional in v1 through an explicit signing-provider interface;
- private keys are never stored by the TrustState authority;
- an unsigned receipt remains hash-verifiable but must not be described as independently signed evidence.

Acceptance evidence:

- two concurrent writers cannot both advance the same generation;
- stale CAS is rejected without mutation;
- process interruption after history commit and before current update recovers deterministically;
- malformed/forked history fails closed;
- current/history rollback is detected when a newer retained chain exists;
- complete-directory rollback limitation is explicitly documented and tested as out-of-bound without an external witness;
- export receipt reproduces exact transition hashes;
- optional receipt signature verifies through the existing qualified signing boundary;
- no TrustState persistence code imports broker, OMS, strategy, risk or live-routing authority.

**Sequencing:** offline CLI v1 -> Persistent TrustState Authority v1 -> contract-first Verification API -> SDK contract -> reference profiles/corpus -> OEM/embedded qualification.

Every step remains bounded by the authority rules in this master plan. None of these layers proves strategy profitability or enables live/mainnet trading.

## Master-plan execution discipline

This document is the mandatory architecture/assurance review source before each significant ASTRA development wave.

Before opening or materially extending an implementation PR:

1. re-read the relevant sections of this master plan;
2. compare the proposed slice with current canonical `main` and active qualification/trust work;
3. check whether an existing planned capability already covers the requirement;
4. preserve all authority boundaries, especially ARM/HALT, paper/live separation, qualification-vs-promotion separation and `PROFITABILITY_NOT_PROVEN`;
5. classify newly discovered strengthening directions as `ADOPT`, `ADAPT`, `REFERENCE`, `CONDITIONAL` or `REJECT`;
6. record source/reference, intended boundary, sequencing/dependencies and acceptance evidence before treating a new direction as part of the roadmap;
7. make implementation PRs reference the relevant master-plan section where practical;
8. when a newer finding supersedes an older idea, preserve the history but mark the older path superseded rather than silently deleting the rationale.

New assurance, qualification, ecosystem or commercial ideas should first be reconciled against this file so ASTRA evolves as one coherent system rather than as disconnected feature additions.

This discipline does not authorise strategy promotion or live/mainnet trading.
