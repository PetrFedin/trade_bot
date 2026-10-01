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

