# ASTRA Trade Bot — End-to-End Readiness Matrix

This document is the human-readable source of truth for product-readiness claims. A capability is not marked PASS merely because code exists; it must have an executable qualification gate and credible evidence. Scores are deliberately conservative: deterministic CI cannot substitute for external broker evidence, authoritative point-in-time strategy data, GitHub server-side enforcement, elapsed soak time or real profitability evidence.

`CURRENT_SYSTEM_STATUS.json` is the companion machine-readable current-state contract. Neither document authorizes Demo or live trading by itself.

## Current system identities — observed 2026-09-02

| Identity | Exact reference | Current status | What it proves / does not prove |
|---|---|---|---|
| Canonical stable core | `main` — last fully re-qualified engineering baseline `0bc1e309e1b268756023088b6e440db30cb4ddab` | `ENGINEERING_BASELINE_PASS` | Covered deterministic/security/OMS/PostgreSQL baseline is green on exact `main`; does not prove external broker connectivity, Demo execution, profitability or live readiness |
| Operational boundary candidate | PR #93 — `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b` | `CODE_QUALIFIED_DEMO_UNPROVEN` | Latest identified operational-only boundary before active research; strong code qualification exists, but no real protected Bybit Demo ENTRY or complete real-broker operational evidence chain is proven |
| Active research head | PR #100 — `918dbc57c0633c6dc549f1f036d2ae659b289b46` | `RESEARCH_ONLY` | Contains current first-touch/derivatives-context research; it is not an operational release candidate and has no strategy-promotion authority |
| Strategy profitability | frozen Bybit price-only research evidence | `PROFITABILITY_NOT_PROVEN` | Latest frozen replay is negative: 102 trades, 36 WIN / 11 BE / 55 LOSS, approximately `-176.67 USDT` on `1,000 USDT` reference equity; incomplete derivatives research does not supersede it |
| GitHub governance | `main` server state + `release/ownership.json` | `VERIFIED_DISABLED` | Branch protection and required-status enforcement are known to be disabled; tracked in #103 |
| Live/mainnet | stable live authority flags + current machine status | `FAIL_CLOSED` | External routing, live trading and mainnet entry remain disabled |

The operational/research stack is not consolidated. Git history quantifies the current boundary: old stable-main baseline `62f32ba73b3ead2592e58ba8c5b46ffc9d266e2b` to PR #93 is a very large 1426-commit delta; PR #93 to PR #100 adds another 50 commits dominated by research work. Consolidation is tracked in #104 and must preserve unique code/evidence rather than blindly merging the stack.

## Current product state

| Capability | Current state | Evidence / gate | Score | To reach or retain >= 8/10 |
|---|---|---|---:|---|
| Architecture cohesion | QUALIFIED STABLE CORE; STACK NOT CONSOLIDATED | stable `domain`, `marketdata`, `strategy`, `risk`, `portfolio`, `oms`, `execution`, `application`, `observability`; one stable composition boundary for SQLite/PostgreSQL paths; covered canonical-main regression green; later operational and research work remains in a very large open stacked lineage | 8.0 | preserve the stable core while consolidating the stacked PR graph into one canonical core, one bounded operational candidate and isolated research; retire duplicated/versioned executable paths only after dependency/evidence preservation is proven |
| Real external integration | BLOCKED / CURRENT CREDENTIAL-BACKED EVIDENCE ABSENT | real-network adapters/workflows exist, but a wrapper workflow completing safely while credential-backed broker steps are skipped is not a broker PASS; Bybit protected Demo path is code-qualified but no real one-shot Demo ENTRY is proven | 4.0 | current credential-backed read evidence; protected Bybit Demo mutation proof; disconnect/recovery evidence; exact-head broker evidence chain and SLO history |
| Trading research framework | STRONG PARTIAL | deterministic/no-lookahead research infrastructure, fees/slippage/drawdown, walk-forward/OOS machinery, manifested observed-price sample and later Bybit outcome/first-touch research tooling exist; current active derivatives-context experiment remains incomplete | 7.0 | authoritative point-in-time data, broader multi-asset/regime coverage, frozen hypotheses/holdout protocol, cost/capacity/stress qualification and prospective validation |
| Strategy profitability | FAIL / NOT PROVEN | latest frozen Bybit price-only replay: 102 trades, 36 WIN / 11 BE / 55 LOSS, approximately `-176.67 USDT` on `1,000 USDT` reference equity; no positive frozen cost-adjusted OOS edge has been established | 2.0 | complete point-in-time research without outcome-driven retuning; freeze candidate; pass untouched OOS, cost, robustness, concentration and drawdown gates before any strategy promotion |
| Portfolio management | QUALIFIED DETERMINISTICALLY | fee-aware cash/positions, realized/unrealized P&L/equity, idempotent fills, broker reconciliation, stock splits/dividends, SQLite replay/snapshots, PostgreSQL append-only event journal, restart replay, snapshots and concurrent duplicate-event idempotency | 8.0 | retain >=8 with real broker reconciliation/manual-action evidence and later production accounting reconciliation; add multi-currency/tax lifecycle only if scope requires it |
| Risk management | QUALIFIED DETERMINISTICALLY | exposure, kill switch, freshness, execution-quality, loss/drawdown/turnover, liquidity, concentration and volatility guardrails plus immutable SQLite/PostgreSQL risk-decision evidence wired into the planning path | 8.0 | retain >=8 through scenario/property tests, explicit production policy governance, margin/liquidation/clock-skew/manual-position controls where applicable, and real connected evidence |
| OMS / execution state | QUALIFIED DETERMINISTICALLY | transactional SQLite/PostgreSQL OMS, row locking, append-only events, deterministic client IDs, durable submit/mutation outboxes, monotonic fills, at-most-one submit/cancel/replace attempt, `STARTED` before mutation, GET-only ambiguity/restart recovery, fencing, broker-ID rotation and repeated fault campaigns | 8.5 | real broker partial-fill/cancel/replace/ambiguity evidence, protection verification, connected restart recovery and operational SLO history |
| Release process | STRONG PARTIAL — SUPPLY CHAIN STRONG; PROTECTION DISABLED | hash-locked dependencies, lock freshness, Ruff/Bandit/dependency audit, exact Action pins, CI supply-chain policy, wheel/sdist + manifest + SPDX SBOM, signed SLSA/SBOM attestations and technical release/rollback ownership exist; `main` protection is verified disabled | 7.9 | enable and independently verify branch protection/required reviews/status checks (#103), consolidate release lineage (#104), execute a version-matched signed release tag, retain independent live approval as a separate gate |
| Demo / paper operational readiness | DETERMINISTICALLY STRONG; REAL BROKER UNPROVEN | stable safety, immutable risk evidence, PostgreSQL durability, at-most-once mutation and recovery controls are qualified; later Bybit one-shot operational design exists; real protected Bybit Demo ENTRY, complete exact-head real-broker chain and connected soak are absent | 7.0 | first protected real Demo ENTRY; exact-head INFRA->SESSION->SUPERVISOR->ARM->ENTRY->HALT->RECOVERY evidence; multi-week connected soak and failure drills |
| Production infrastructure | SOURCE/CI QUALIFICATION ONLY | V109 status keeps KMS/HSM, workload identity, Kubernetes mutation authority and live routing false; production PostgreSQL backup/restore, external audit and related external evidence are not complete | 4.0 | prove production signer/workload identity, backup/restore, external audit, deployment/rollback, observability and incident response against the actual target environment |
| Live trading readiness | BLOCKED | strategy profitability, real Demo, exact-head broker evidence, connected soak, server-side governance, production external evidence and independent live approval remain incomplete | 2.0 | every live-critical gate independently PASS; controlled tiny-capital pilot only after those prerequisites; no generic config flag may bypass them |
| Live profitability | NOT STARTED / UNPROVEN | no authorized tiny-capital mainnet pilot and therefore no statistically meaningful real net-performance evidence | 0.0 | only after a separately approved live pilot: reconciled real fills/costs, sufficient sample/regime coverage, acceptable drawdown and defensible net expectancy |

## Verified evidence snapshots

Evidence snapshots below are historical or current exact-run records. A historical PASS remains useful evidence for the subsystem tested, but it is not automatically fresh evidence for a later release head.

### Current canonical-main engineering baseline repair

Canonical `main` after the 2026-09-02 baseline repair:

- commit: `0bc1e309e1b268756023088b6e440db30cb4ddab`;
- `order-lifecycle-fault-campaign` run: `33653750467` — success;
- `canonical-security-regression` run: `33653750562` — success;
- ordinary full regression runs with a real PostgreSQL DSN instead of silently blanking it;
- canonical security migration lineage `v107 -> v108 -> v109` is compared with frozen platform assets and applied twice before full PostgreSQL regression;
- complete regression result: **1044 passed / 2 dedicated fleet-deployment skips**;
- the two generic skips require `ASTRA_TEST_FLEET_DEPLOYMENT_DSN` and are owned by the separate `canonical-deployment-regression`, rather than being counted as ordinary PostgreSQL PASS;
- repeated PR campaign qualification before merge: 25 SQLite cycles and 8 PostgreSQL mutation/fencing cycles;
- scheduled configuration remains 100 SQLite cycles and 25 PostgreSQL cycles;
- retained current-main campaign artifact: `9855869225`;
- artifact digest: `sha256:ddc4848a53d1a151c74ce1cd261e9894a2ac47e3ba179b56be427dd6f17b6c49`;
- fail-closed V109 live-state assertions remain PASS.

This repair closed a real qualification-integrity defect: the old full-regression step explicitly suppressed the PostgreSQL DSN, hiding ordinary PostgreSQL tests. Enabling the database first exposed missing V109 schema initialization; the fix reused the already-canonical V107->V109 migration lineage rather than adding ad-hoc schema SQL. The resulting exact-main PASS is stronger than the earlier campaign snapshot but still does not substitute for real broker/Demo/soak evidence.

### Dedicated fleet-deployment PostgreSQL qualification

The generic full regression intentionally leaves two fleet-deployment tests to their dedicated owner because they require a distinct `ASTRA_TEST_FLEET_DEPLOYMENT_DSN` contract. `canonical-deployment-regression.yml` provisions PostgreSQL 16, applies V105/V106 deployment migrations twice, runs the real PostgreSQL fleet-deployment integration, focused/full regression, coverage floors, durable DB contract checks and fail-closed deployment assertions.

A previously retained successful `main` run exists for that dedicated contour. It is evidence that the fleet skips are not orphaned; it is **not** represented as a fresh rerun on `0bc1e309...` because the baseline repair did not change the V105/V106 fleet-deployment surfaces.

### Manifested observed-price strategy qualification

Historical canonical `main` evidence after benchmark-correctness repair:

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

The original acceptance thresholds were not relaxed during that benchmark repair: target quantity 1, opening cash 10,000, $0.50 fee per fill, 5 bps slippage, 10/5/5 train/test/step bars, two windows per regime, 10% maximum drawdown, and -3% floors for mean OOS and mean excess return. The policy additionally requires versioned benchmark semantics.

Observed historical regime results:

- `rising_2015_q4`: mean strategy OOS `-0.000152531749925`, mean capital-matched benchmark `-0.000152531749925`, mean excess `0E-14`, worst drawdown `0.0005791003`, 2 trades;
- `drawdown_2016_spring`: mean strategy OOS `0`, mean capital-matched benchmark `-0.000494520050075`, mean excess `0.000494520050075`, worst drawdown `0`, **0 trades**;
- `range_2016_q4`: mean strategy OOS `-0.00005804210000`, mean capital-matched benchmark `0.00015406379995`, mean excess `-0.00021210589995`, worst drawdown `0.00010558650015`, 2 trades.

This is evidence that qualification machinery is reproducible on an observed-price sample under predeclared costs/benchmark semantics. It is **not** evidence of a profitable production strategy: the sample is one asset / 60 observations, one regime qualifies by avoiding trades, and the source is intentionally non-authoritative.

### Current frozen Bybit research truth

The later Bybit research line is more relevant to the current strategy question than the historical AAPL validation sample, but it remains research rather than production evidence.

Latest frozen price-only evidence records:

- 2,394 eligible signals;
- 792 plan-eligible signals at 1,000 USDT reference equity;
- 617 independent target/stop episodes;
- 137 `TARGET_FIRST` (22.20%);
- 471 `STOP_FIRST`;
- 9 `NEITHER`;
- LONG `TARGET_FIRST`: 27.13%;
- SHORT `TARGET_FIRST`: 13.91%;
- of 471 STOP_FIRST episodes, 315 later reached at least 1R favorable and 204 later reached at least 2R within 240 minutes;
- portfolio replay: 102 trades — 36 WIN / 11 BE / 55 LOSS;
- net replay result: approximately `-176.67 USDT` on `1,000 USDT` reference equity (approximately `-17.67%`);
- 51 HARD_STOP trades contributed approximately `-464.25 USDT`;
- no supported 100% price-only cross-token predictive pattern was established.

Therefore the current strategy cannot be called profitable. STOP_FIRST outcomes are heterogeneous and justify point-in-time timing/regime/context research, but they do not justify simply widening stops, disabling a direction, adding filters or retuning after seeing outcomes.

The active derivatives-context experiment is intended to test pre-entry open-interest direction, account-ratio crowding, prior settled funding and frozen stress-regime context. Public Bybit V5 acquisition from GitHub-hosted infrastructure has been blocked by HTTP 403/CloudFront geography restrictions. Until protected fixed-egress acquisition produces a complete point-in-time frozen result, the derivatives hypothesis remains **incomplete** and cannot promote the strategy.

### Deterministic order-lifecycle fault campaign — historical predecessor

Earlier canonical evidence before the current baseline repair:

- commit: `8dce54a1cf1eccfc7152ef141619f35c8a1e0e81`;
- workflow run: `31335919400`;
- status: `success`;
- PR/push campaign strength: 25 repeated SQLite cycles and 8 PostgreSQL mutation-durability/fencing cycles;
- retained artifact: `9044324636`;
- artifact digest: `sha256:f0731e880fadc75b3333702c4cb72bc60fd683a3b57cfb39c6efe6ead1fa6869`.

This remains historical evidence for the mutation invariants. The 2026-09-02 exact-main baseline above supersedes it as the current order-lifecycle qualification reference because the newer run also restores ordinary PostgreSQL full-regression coverage.

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

Canonical evidence with live branch-summary drift verification records:

- technical artifact release owner: `@PetrFedin`;
- technical rollback owner: `@PetrFedin`;
- independent live approver: unassigned;
- branch-protection verification: `VERIFIED_DISABLED`;
- observed `main` protection: disabled;
- observed required status-check enforcement: `off`;
- live release allowed: `false`.

`release/ownership.json` remains the machine contract for this boundary. The release-governance workflow may detect drift, but repository-side validation is not a substitute for actually enabling server-side protection. Issue #103 tracks that P0 gap.

## Current deterministic E2E chain

```text
validated market data
  -> data-quality / freshness gate
  -> strategy target
  -> portfolio intent / sizing
  -> pre-trade risk evaluation
  -> immutable risk-decision evidence
  -> deterministic client order id
  -> durable OMS create
  -> durable risk approval
  -> durable submit outbox
  -> persist SUBMIT_STARTED before network mutation
  -> at-most-one broker mutation attempt
  -> GET-only ambiguity/restart recovery
  -> broker acknowledgement / monotonic fill state
  -> optional durable cancel/replace request
  -> persist mutation STARTED before DELETE/PATCH
  -> at-most-one cancel/replace attempt
  -> GET-only mutation ambiguity/restart recovery
  -> append-only portfolio/accounting events
  -> cash / position / realized P&L / unrealized P&L / income / equity
  -> deterministic replay / snapshot
  -> read-only broker-truth reconciliation
  -> safety / operational readiness state
```

The stable deterministic chain is not the same thing as the later protected Bybit operational activation chain. The latter additionally depends on exact operational-head identity, fixed egress, credential/readiness checks, persistent supervisor state, exact ARM, short-lived approval, authorization provenance, position protection, HALT and controlled recovery.

## PostgreSQL qualification evidence

Dedicated PostgreSQL 16 gates validate OMS row locking, durable outbox behavior, duplicate broker-event idempotency, monotonic fills and append-only OMS events. Mutation qualification validates active-mutation fencing, cancel/replace persistence, broker-ID rotation and concurrent conflicts. Separate PostgreSQL gates validate immutable risk evidence and append-only portfolio journals.

The repaired canonical fault campaign now additionally ensures ordinary full regression runs with PostgreSQL enabled and initializes the canonical V107->V109 security migration lineage before that regression. This prevents a clean-database schema omission from being hidden by an empty DSN.

SQLite remains useful for deterministic/local qualification but is not the sole durable backend for OMS, mutations, risk evidence or portfolio state.

## External broker boundary

### Alpaca

The repository contains a separate real-network Alpaca Paper read-only qualification workflow. It remains intentionally separate from deterministic CI.

A workflow run that completes its wrapper safely while credential-dependent broker steps are skipped because credentials are unavailable is **not** evidence that Alpaca REST/WebSocket connectivity succeeded. Real external Alpaca integration remains unqualified until current credential-backed evidence is retained.

### Bybit

The unmerged operational line contains significantly stronger protected mutation controls than the stable-main Alpaca read-only story, including a one-shot Demo entry path and exact-head release-evidence design. However:

- no real protected Demo order has been sent through that new path;
- no complete real-broker `INFRA -> SESSION -> SUPERVISOR -> ARM -> ENTRY -> HALT -> RECOVERY` chain is proven on one exact current operational head;
- no connected multi-week Demo soak is proven;
- the current strategy itself has not passed a profitability promotion gate.

Therefore Bybit operational code qualification must not be described as Demo/live readiness.

## Release-governance boundary

Repository-side provenance, dependency lock, security, Action-pin, release-evidence and technical ownership controls are implemented and qualified. GitHub server-side `main` branch protection is positively verified as **disabled**, not merely unknown. Detailed required-review/ruleset enforcement must not be claimed until it is actually enabled and independently read back. Independent live approval also remains unassigned.

## Hard production/live blockers

Production/mainnet routing must remain disabled while any required gate below is unresolved:

- positive cost-adjusted frozen OOS strategy edge is not proven; the latest frozen Bybit price-only replay is negative;
- authoritative point-in-time derivatives-context acquisition/qualification is incomplete;
- no protected real Bybit Demo ENTRY exists through the current operational path;
- no complete exact-head real-broker operational evidence chain exists;
- no multi-week connected Demo soak / broker fault-recovery evidence exists;
- the operational and research stacked PR lineage has not been consolidated (#104);
- GitHub `main` branch protection / required-review enforcement is verified disabled (#103);
- production KMS/HSM identity, workload identity, backup/restore, external audit and related target-environment evidence remain incomplete;
- independent live approver is unassigned;
- no controlled tiny-capital mainnet pilot has been authorized or completed;
- consequently no live profitability evidence exists.

These blockers are cumulative. Passing one does not waive another.

## Definition of >=8/10

A score of 8 or above requires implemented behavior, executable automated verification, realistic environment evidence where applicable, documented failure behavior, explicit operational ownership and no known P0/P1 correctness gap in that capability. A deterministic score of 8 in one subsystem never overrides missing external or profitability evidence elsewhere and never authorizes live trading by itself.

## Definition of `PRODUCTION_PROFITABLE_VALIDATED`

The repository must not use that status until all of the following are independently evidenced on appropriately bound versions/heads:

1. software correctness;
2. data integrity / point-in-time correctness;
3. frozen positive cost-adjusted OOS strategy evidence;
4. real connected Demo execution;
5. exact-head operational evidence;
6. connected soak and failure recovery;
7. production security/governance/deployment/backup/observability readiness;
8. independent approval of a tiny-capital mainnet pilot;
9. reconciled real live fills/costs/accounting;
10. sufficient live sample/regime coverage showing positive net performance within the approved drawdown/risk policy.

Even then, the status is evidence about observed past/ongoing performance and operational qualification, not a guarantee of future profit.
