# ASTRA — Research Signal Trust & Competitive Capability Roadmap

**Scope:** research evidence only. **Release authority:** HOLD. **No broker, Demo, mainnet or live trading permissions.**
**Reference branch:** research/signed-signal-capability-gates-20261020, stacked onto PR #327.
**Single source for statuses:** research/competitive_capability_register_v1.json.

## Verified product benchmarks (official vendor docs)

| Product | Publicly documented function | Development direction in ASTRA | Primary documentation |
|---|---|---|---|
| 3Commas | Signal Bot receives custom TradingView or third-party JSON signals | Trusted relay, limited signed research observations, explicit reject reasons | https://help.3commas.io/en/articles/16281112-signal-bot-comprehensive-json-guide |
| Veles (Russia) | TradingView webhook filters can combine with bot indicators | Research signal filters and quality diagnostics without broker actions | https://help.veles.finance/ru/filters/tradingview/ |
| Hummingbot | Controllers and executors, bot orchestration, position summaries | Read-only multi-bot supervisor and orphan-position diagnostics | https://hummingbot.org/hummingbot-api/routers/ |
| Freqtrade | Backtesting and lookahead-bias diagnostics | Leakage-resistant walk-forward and independent strategy dossier | https://docs.freqtrade.io/en/stable/strategy-customization/ |

Benchmarks indicate potential product gaps, not code reuse permission, deployed feature parity or trading approval.

## Implemented, isolated research-only signal boundary

The tools/research_signal_gate.py module is **not** a webhook server. Its trusted caller must supply a canonical UTF-8 JSON body, trusted HMAC signing key configuration, signature, UTC time and private SQLite journal.

The module enforces:
- strict schema, canonical byte encoding and no unknown fields;
- independent HMAC-SHA256 signing domain and approved key identifier;
- per-signer approved source, strategy and symbol;
- bounded signal age, future skew and expiry;
- durable duplicate refusal by (source, signal_id), including key rotation;
- no orders, position mutations, Demo privileges or live-trading authority.

Only RECORDED_RESEARCH_ONLY is a possible positive result. Verification and journal persistence do not certify an exchange fill or strategy profitability.

### Trusted relay caveat

TradingView alerts do not automatically include ASTRA-specific HMAC signatures. A future network relay must first establish the original event authenticity using mechanisms the provider actually supports. Signing an unauthenticated inbound message with an ASTRA key does not authenticate the original publisher.

**Not implemented:** public HTTP ingress, real TradingView connection, multi-tenant account access or execution of alerts.

## End-to-end product chain

| Stage | What the user will be able to do | Current state | Independent acceptance |
|---|---|---|---|
| Signal Inbox | Browse trusted, stale, invalid and replayed messages | RESEARCH GATE ONLY | Exact-head CI, concurrent replay and secret-safe diagnostics |
| Trusted webhook relay | Connect remote signal sources | PLANNED | Original-source proof, throttling, tenant fencing, secrets rotation |
| Strategy Workbench | Build and version entry/exit/risk experiments | PLANNED UI | Reproducible strategy spec, read-only preview and hypotheses |
| Historical data dossier | Check source validity and settlement timing | MARK PRICE BLOCKED | Pinned real venue data and independent reconstruction |
| PnL/funding laboratory | Reproduce costs, exposure, exits and scenarios | EXISTING RESEARCH CORE | Same verified inputs must give identical results |
| Evidence Passport | Inspect experiment, code SHA, data digest and assumptions | PARTIAL | Independently verified immutable evidence and complete coverage |
| Bot supervisor | View health, state, reconciliation and orphan exposure | PLANNED UI | No trading authority granted by a screen or label |
| Operator/reviewer views | Distinguish proven vs blocked and export incident detail | PARTIAL STATIC | Role-scoped UI and 375/768/1440 browser acceptance |

States in this table are engineering descriptions, not automatic readiness classification. The machine-readable registry contains the explicit feature-by-feature current statuses.

## Differentiators to build, not yet claim

**Evidence Passport:** one dossier ties original provider facts, code SHA, data SHA, assumptions, train/test windows, funding, fees and independent auditor output together.

**Why Denied?:** clear non-sensitive reasons: stale bars, invalid source, invalid signature, duplicate ID, conflicting broker facts, unapproved risk or unavailable Mark Price. Avoid revealing signing secrets.

**Counterfactual Risk Lens:** compare research-only portfolios under alternate size, slippage, fees, liquidation assumptions and stress regimes while retaining the exact baseline.

**Contradiction Monitor:** display disagreement between modeled PnL, actual execution evidence and funding ledger instead of replacing unknown provider facts with estimates.

**Strategy Experiment Registry:** separate in-sample search from untouched out-of-sample and repeatable walk-forward evidence. No hyperparameter search on a reused holdout may count as independent validation.

## Operating journeys

Researcher: choose dataset -> verify quality -> register hypothesis -> configure strategy -> run offline backtest -> inspect fee/funding and regime sensitivity -> obtain independent evidence.

Operator: inspect source freshness -> review trusted/rejected signals -> check HALT and reconciliation -> open incident dossier. During HOLD, submission controls are absent or disabled.

Investor/auditor: open capability status -> inspect evidence for precise code SHA -> compare assumptions and verified calculations -> review unresolved blockers and falsifiable release gates.

## Priorities and acceptance gates

1. **P0** Exact-head tests and independent review of research signal and capability registry.
2. **P0** Trustworthy webhook relay and isolated original-source authentication before real signal intake.
3. **P0** Obtain provider-origin Mark Price through permitted sources; no substitution for HTTP 403.
4. **P1** Leakage-aware strategy workbench, experiment registry, risk attribution and immutable evidence dossiers.
5. **P1** Read-only operator UX with 375/768/1440 screenshots and evidence-led navigation.
6. **P2** Multi-bot research supervisor, adapter conformance, GRID/DCA test scenarios and tenant access controls.

Engineering CI cannot enable Demo, mainnet, live routing or production release. Those are separate authorities with independently reviewed evidence.
