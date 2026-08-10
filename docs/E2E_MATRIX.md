# ASTRA Trade Bot — End-to-End Readiness Matrix

This document is the source of truth for product-readiness claims. A capability is not marked PASS merely because code exists; it must have an executable qualification gate and credible evidence. Scores are deliberately conservative: deterministic CI cannot substitute for external broker evidence, authoritative historical-data coverage, GitHub server-side enforcement or elapsed soak time.

## Current product state

| Capability | Current state | Evidence / gate | Score | To reach or retain >= 8/10 |
|---|---|---|---:|---|
| Architecture cohesion | QUALIFIED DETERMINISTICALLY | stable `domain`, `marketdata`, `strategy`, `risk`, `portfolio`, `oms`, `execution`, `application`, `observability`; one composition root for SQLite and PostgreSQL; shared durable OMS, order-mutation, risk and portfolio stores; consolidated `main`; complete regression green | 8.0 | retain >=8 by retiring duplicated versioned domain/runtime models and keeping broker/deployment adapters behind the stable composition boundary |
| Real external integration | BLOCKED BY CREDENTIAL EVIDENCE | real Alpaca Paper REST + WebSocket workflow is implemented and was actually invoked; it failed at the explicit credential gate before network access because repository Alpaca secrets are absent | 4.0 | configure protected Paper credentials; repeated successful REST/WebSocket evidence; controlled Paper mutation drill; disconnect/recovery evidence and broker SLO history |
| Trading logic | STRONG PARTIAL — MANIFESTED SAMPLE + CAPITAL-MATCHED BASELINES | deterministic long-only validation strategy; no-lookahead next-bar backtester; fees/slippage/drawdown; walk-forward OOS windows; hash-locked 60-row AAPL observed-price snapshot across three named regimes; upstream Git blob + snapshot/canonical SHA provenance; predeclared strategy thresholds; versioned `capital_matched_buy_hold_v1` acceptance benchmark using the same quantity/capital/fee/slippage; raw asset and cash baselines retained separately; canonical-main evidence | 7.0 | authoritative/licensed historical source; broader multi-asset and multi-cycle regime coverage; independently sourced benchmark data where appropriate; predeclared activity/trade-coverage requirements; production-calibrated acceptance thresholds |
| Portfolio management | QUALIFIED DETERMINISTICALLY | fee-aware cash/positions, realized/unrealized P&L/equity, idempotent fills, broker reconciliation, stock splits/dividends, SQLite replay/snapshots, PostgreSQL 16 append-only event journal, restart replay, snapshots and concurrent duplicate-event idempotency | 8.0 | retain >=8 with real broker corporate-action/reconciliation evidence; add multi-currency/tax lifecycle only when product scope requires it |
| Risk management | QUALIFIED DETERMINISTICALLY | exposure, kill switch, freshness, execution-quality, loss/drawdown/turnover, liquidity, concentration and volatility guardrails plus immutable SQLite/PostgreSQL risk-decision hash chain wired into the actual paper planning path; PostgreSQL concurrent writers serialize through a locked chain head | 8.0 | retain >=8 through scenario/property testing and production-calibrated thresholds; real Paper evidence remains a separate paper-readiness gate |
| OMS / execution state | QUALIFIED DETERMINISTICALLY | transactional SQLite + PostgreSQL 16 OMS, row locking, append-only events, deterministic client IDs, durable submit and mutation outboxes, monotonic fills, at-most-one submit, at-most-one cancel/replace attempt, `STARTED` before broker mutation, GET-only ambiguity/restart recovery, active-mutation fencing, broker-ID rotation handling, read-only reconciliation and repeated deterministic fault campaigns on canonical `main` | 8.5 | retain >=8 with real broker cancel/replace fault evidence and operational SLO history; scheduled deterministic campaigns support regression confidence but do not replace external soak evidence |
| Release process | STRONG PARTIAL — SIGNED PROVENANCE + OWNERSHIP; PROTECTION DISABLED | canonical `main`, stable package identity, hash-locked `requirements.lock`, lock freshness verification, Ruff/Bandit/dependency audit, exact Node24-native GitHub Action SHA pins, CI supply-chain policy, wheel/sdist + release manifest + SPDX SBOM, signed SLSA build provenance and signed SBOM attestation, machine-validated artifact release/rollback ownership; GitHub branch summary reports `main` protection disabled and required status checks off, with drift checked in CI | 7.9 | enable and independently verify server-side branch protection/required reviews; execute a signed version-matched release tag; preserve externally verifiable attestations; keep independent live approval separate from technical artifact ownership |
| Paper-operation readiness | STRONG PARTIAL | paper safety, trading E2E, immutable risk evidence, PostgreSQL portfolio/OMS durability, at-most-once submit, deterministic cancel/replace recovery, operational SLO and composition-root gates pass on `main`; repeated fault campaign also passes on `main` across SQLite and PostgreSQL | 7.8 | credentialed Alpaca Paper read-only PASS; controlled real Paper mutation/recovery drill; 14–30 day external soak with clean reconciliation/SLO evidence |
| Live trading readiness | BLOCKED | all product qualification paths keep external/live routing disabled; real Paper prerequisites, server-side release governance and independent live-pilot controls are incomplete | 2.0 | every live-critical upstream capability >=8 with external evidence; independent approval; tiny-capital pilot controls; production secrets/KMS; real-time reconciliation; global kill switch |

## Verified evidence snapshots

### Manifested observed-price strategy qualification

Canonical `main` evidence after benchmark-correctness repair:

- commit: `b1bb27d79fdf50d0885d1cae2e3b1837c6746ab8`;
- workflow run: `31441661579`;
- status: `success`;
- dataset: AAPL, 60 daily observations split into three non-overlapping 20-session windows;
- dataset canonical SHA-256: `4242262f4d5d79352a43ec5cdc81f3a9d52953fd5ca2f230b3fabf890d33a256`;
- manifest SHA-256: `b5ba54d458413e68d94771cb5c475e33e3834038c2f678e733916107ef8cb0be`;
- upstream repository/path: `plotly/datasets` / `finance-charts-apple.csv`;
- upstream Git blob: `7b1bab3953bb5cdf47e84de1048ca04b0c991987`;
- source classification: `THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE`;
- benchmark mode: `capital_matched_buy_hold_v1`;
- retained artifact: `9083061322`;
- artifact digest: `sha256:c05cc14a45e8d6e43f0ffed79de98ab28a8e7c821bc9be6a76cd04ec4805a155`.

The original acceptance thresholds were not relaxed during the benchmark repair: target quantity 1, opening cash 10,000, $0.50 fee per fill, 5 bps slippage, 10/5/5 train/test/step bars, two windows per regime, 10% maximum drawdown, and -3% floors for mean OOS and mean excess return. The policy now additionally requires the versioned benchmark semantics.

The acceptance benchmark is capital-matched buy-and-hold: it buys the same target quantity at the first OOS close using the same entry slippage and fee, leaves unused capital in cash, and marks the holding to the final OOS close. Raw AAPL price return is evidence only; it is no longer directly subtracted from a one-share strategy portfolio return. A zero-return cash control is also emitted.

Observed regime results from the corrected canonical evidence:

- `rising_2015_q4`: mean strategy OOS `-0.000152531749925`, mean capital-matched benchmark `-0.000152531749925`, mean raw-asset return `-0.00760026780686976355577945215`, mean excess `0E-14`, worst drawdown `0.0005791003`, 2 trades;
- `drawdown_2016_spring`: mean strategy OOS `0`, mean capital-matched benchmark `-0.000494520050075`, mean raw-asset return `-0.04182874335654989023420567406`, mean excess `0.000494520050075`, worst drawdown `0`, **0 trades**;
- `range_2016_q4`: mean strategy OOS `-0.00005804210000`, mean capital-matched benchmark `0.00015406379995`, mean raw-asset return `0.01985912869689507644215529458`, mean excess `-0.00021210589995`, worst drawdown `0.00010558650015`, 2 trades.

The benchmark repair materially changes interpretation. For example, the drawdown regime no longer reports roughly +4.18% excess for a one-share strategy merely because the asset fell roughly 4.18%; the comparable excess is about +0.049% of portfolio capital. Likewise the range-regime excess is about -0.021%, not -1.99%.

This is evidence that qualification machinery is reproducible on observed prices under predeclared costs, thresholds and benchmark semantics. It is **not** evidence of a profitable production strategy: the strategy is approximately flat in these windows, one regime qualified by avoiding trades, the sample contains one asset and only 60 observations, and the source is intentionally classified as non-authoritative.

### Deterministic order-lifecycle fault campaign

Canonical `main` evidence:

- commit: `8dce54a1cf1eccfc7152ef141619f35c8a1e0e81`;
- workflow run: `31335919400`;
- status: `success`;
- PR/push campaign strength: 25 repeated SQLite submit/mutation cycles and 8 repeated PostgreSQL mutation-durability/fencing cycles;
- retained artifact: `9044324636`;
- artifact digest: `sha256:f0731e880fadc75b3333702c4cb72bc60fd683a3b57cfb39c6efe6ead1fa6869`;
- nightly configuration: 100 SQLite cycles and 25 PostgreSQL cycles.

This evidence increases deterministic confidence in at-most-once submit/cancel/replace behavior, GET-only ambiguity/restart recovery, monotonic fill adoption and durable mutation fencing. It is not a substitute for a credentialed broker mutation drill or elapsed external soak time.

### Signed release provenance

First qualified signed provenance on canonical `main`:

- commit: `3dc298b4f6d8fba504e560762d101cae6d4070bc`;
- workflow run: `31336212403`;
- SLSA build-provenance attestation ID: `39704670`;
- signed SBOM attestation ID: `39704671`;
- retained evidence artifact: `9044407752`;
- artifact digest: `sha256:50638e11323333a99e46dca19b39a57901986b5bb6392353232b94824136f2c3`.

The attestation job runs only after trusted `main`/tag pushes and verifies SHA-256 checksums after artifact transfer and before signing. Pull-request qualification remains read-only and does not receive OIDC/attestation write privileges.

### Release ownership and branch-state governance

Canonical `main` evidence with live branch-summary drift verification:

- commit: `9cf92a9993d9fd54896e5696115f673633d3ac2a`;
- workflow run: `31440945455`;
- status: `success`;
- technical artifact release owner: `@PetrFedin`;
- technical rollback owner: `@PetrFedin`;
- independent live approver: unassigned;
- branch-protection verification: `VERIFIED_DISABLED`;
- observed `main` protection: disabled;
- observed required status-check enforcement: `off`;
- live release allowed: `false`;
- retained evidence artifact: `9082811204`;
- artifact digest: `sha256:deb125851a0ca2db9486c342b723cb957cde2b87cb759d3d510423a8c1d5f672`.

The `release-governance` workflow re-queries the GitHub `main` branch summary on every qualifying PR/main run and fails on drift. The dedicated protection-detail endpoint is not used as proof, so detailed CODEOWNER/repository-ruleset claims are not inferred.

## Security qualification

The stable product core is required to pass Ruff, Bandit, dependency auditing, compile checks and the complete deterministic pytest regression. During the hardening cycle, `pip-audit` found `PYSEC-2026-3552` in the installed `cryptography 49.0.0` line. The project dependency contract and V108 architecture audit were upgraded to require `cryptography>=50,<51` instead of suppressing the advisory. Current release and product gates install the hash-locked dependency graph, require exact Node24-native GitHub Action commit SHAs for operational workflows, generate signed build/SBOM attestations on trusted `main`/tag pushes, validate release ownership, and keep live/production execution flags fail-closed.

## Current deterministic E2E chain

```text
validated market data
  -> data-quality gate
  -> deterministic strategy target
  -> order intent
  -> pre-trade risk evaluation
  -> immutable risk-decision evidence (approved or rejected)
  -> deterministic client order id
  -> durable OMS create
  -> durable risk approval
  -> durable submit outbox
  -> persist SUBMIT_STARTED before network mutation
  -> at-most-one Paper POST
  -> GET-only ambiguity/restart recovery
  -> broker acknowledgement / monotonic fill state
  -> optional durable cancel/replace request
  -> persist mutation STARTED before DELETE/PATCH
  -> at-most-one Paper DELETE/PATCH
  -> GET-only mutation ambiguity/restart recovery
  -> preserve effective limit price / rotated broker order id
  -> append-only portfolio event journal
  -> cash / position / realized P&L / unrealized P&L / income / equity
  -> deterministic portfolio replay / snapshot
  -> read-only broker-truth reconciliation
  -> operational readiness / SLO gate
```

## PostgreSQL qualification evidence

Dedicated PostgreSQL 16 gates validate OMS row locking, durable outbox behavior, duplicate broker-event idempotency, monotonic fills and append-only OMS events. The order-mutation gate additionally validates migration replay, active-mutation fencing, cancel/replace persistence, broker-ID rotation, concurrent request conflicts and an append-only mutation-event trigger on a real PostgreSQL service. The order-lifecycle fault campaign repeatedly reuses these qualified mutation scenarios against PostgreSQL 16. Separate PostgreSQL 16 gates validate immutable risk evidence with concurrent writers and an append-only portfolio journal with replay, snapshots and concurrent duplicate-event suppression. SQLite remains useful for deterministic/local qualification but is no longer the only durable backend for OMS, order mutations, risk evidence or portfolio state.

## External Paper chain

The repository contains a separate real-network qualification workflow. It is intentionally not conflated with deterministic CI.

```text
GitHub environment: alpaca-paper
  -> explicit Alpaca Paper credentials
  -> TLS-verified Alpaca Paper REST account/open-order reads
  -> Alpaca Paper trade_updates WebSocket authentication/listen handshake
  -> redacted evidence artifact
  -> assert writes disabled
  -> assert external_order_routing_allowed = false
  -> assert live_trading_allowed = false
```

The workflow has been invoked in GitHub Actions, but the repository currently exposes no required Alpaca secrets to the job. It therefore fails at the credential preflight before making a broker network request. Until a credentialed run succeeds, **real external integration remains unqualified**.

## Release-governance boundary

Repository-side provenance, lock, dependency, action-pin, release-evidence and technical ownership controls are executable and qualified. The GitHub `main` branch summary provides positive evidence that server-side branch protection is currently **disabled**, rather than merely unverified. The governance workflow continuously compares the recorded expectation with the GitHub branch summary. Required review / CODEOWNER / detailed ruleset state is not claimed from the unavailable protection-detail endpoint. Independent live approval also remains unassigned. See `docs/RELEASE_GOVERNANCE.md`.

## Hard live blockers

Live routing must remain disabled while any of these are true:

- external Paper REST/WebSocket qualification has no successful credentialed evidence;
- no controlled stable-OMS cancel/replace mutation/recovery drill exists against the real Paper broker;
- no 14–30 day external Paper soak has completed cleanly;
- no authoritative, representative multi-asset/multi-cycle historical-data strategy qualification exists; the current 60-row AAPL sample is deliberately non-authoritative and insufficient for production strategy claims;
- strategy activity/trade-coverage requirements are not yet a separately predeclared acceptance contract; the current drawdown regime has zero trades;
- GitHub `main` server-side branch protection is currently disabled and required-review enforcement is not qualified;
- no version-matched release tag has completed the signed provenance path;
- independent live approver / live-pilot approval and tiny-capital controls are absent.

## Definition of >=8/10

A score of 8 or above requires implemented behavior, executable automated verification, realistic environment evidence where applicable, documented failure behavior, explicit operational ownership and no known P0/P1 correctness gap in that capability. A deterministic score of 8 in one subsystem never overrides missing external evidence in another subsystem and never authorizes live trading by itself.
