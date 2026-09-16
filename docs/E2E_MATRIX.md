# ASTRA Trade Bot — End-to-End Readiness Matrix

This is the **current** human-readable readiness authority. Historical detail that was current before C2A0 remains preserved byte-for-byte in `docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md`.

`CURRENT_SYSTEM_STATUS.json` is the companion machine-readable status. Neither file authorizes Demo or live trading.

## Current system identities — observed 2026-09-06

| Identity | Exact reference | Status | What is actually proven |
|---|---|---|---|
| Qualified product/security main | `ff684ab85b92151c215c7d5cc85bfc527fecb5eb` | `ENGINEERING_BASELINE_PASS` | C2B0 merge SHA completed 11/11 post-merge workflows successfully; not broker/profit/live proof |
| C2A0 runtime lease | PR #113 | `EXTRACTED_AND_QUALIFIED` | Strategy-free v119 singleton runtime lease |
| C2A1 runtime-role boundary | PR #116 | `EXTRACTED_AND_QUALIFIED` | Non-owner least-privilege v119 runtime role |
| C2A2 append-only audit | PR #118 | `EXTRACTED_AND_QUALIFIED` | Frozen v120/001 plus forward physical TRUNCATE hardening |
| C2A3 typed persistence | PR #121 | `EXTRACTED_AND_QUALIFIED` | Strategy-free immutable v120 authorization/provenance/terminal persistence |
| C2A4 active-excursion CAS | PR #124 | `EXTRACTED_AND_QUALIFIED` | Neutral v119 active checkpoint persistence with CAS/tamper rejection |
| C2B0 ARM/HALT control journal | PR #127 / issue #125 | `EXTRACTED_AND_QUALIFIED` | Byte-preserved v121/001, forward no-TRUNCATE hardening, neutral tamper-evident journal, exact reader/writer roles |
| Current consolidation gate | C2B1 / issue #130 | `AUDIT_IN_PROGRESS` | Extract strategy/accounting/broker-free v122 restart-safe session-risk persistence |
| Historical operational source | PR #93 / `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b` | `DECOMPOSITION_SOURCE_DEMO_UNPROVEN` | Preservation/decomposition source only |
| Active research head | PR #100 / `918dbc57c0633c6dc549f1f036d2ae659b289b46` | `RESEARCH_ONLY` | Incomplete derivatives-context research; no promotion authority |
| Strategy profitability | frozen price-only evidence | `FAIL / NOT PROVEN` | 102 trades, 36 WIN / 11 BE / 55 LOSS, about `-176.67 USDT` on 1,000 USDT |
| GitHub governance | current server state | `VERIFIED_DISABLED` | `main` protection / required-status enforcement disabled; #103 |
| Live/mainnet | canonical fail-closed flags | `FAIL_CLOSED` | External routing, live trading and mainnet entry disabled |

## Current capability matrix

| Capability | Current state | Evidence / blocker |
|---|---|---|
| Architecture cohesion | STRONG CORE; CONSOLIDATION IN PROGRESS | C2A0–C2B0 canonical; v122/session/supervisor/recovery layers still bounded work |
| Software correctness | CURRENT BASELINE PASS | C2B0 exact-head 10/10 PR workflows and 11/11 post-merge push workflows SUCCESS |
| Strategy profitability | FAIL / NOT PROVEN | Frozen price-only replay remains negative |
| Database runtime security | C2A1–C2B0 QUALIFIED | Non-owner v119/v120/v121 roles and physical append-only protections proven on PostgreSQL 16 workflows |
| Durable ARM/HALT journal | QUALIFIED | C2B0 preserves exact v121 event identities, neutral ARM evidence semantics and fail-closed trigger/hash checks |
| Session-risk durability | C2B1 AUDIT IN PROGRESS | Historical v122 persistence is useful but domain layer imports strategy risk state and post-trade accounting builders |
| Connected session start | BLOCKED / LATER C2B2 | PR #85 uses fixed-egress authenticated Demo read-only evidence and remains outside persistence-only C2B1 |
| Terminal risk/checkpoint handoff | BLOCKED / LATER C2B3 | PR #86 ordering waits for canonical v122 persistence |
| Real external integration | BLOCKED | No current credential-backed broker proof sufficient for release |
| Demo execution | NOT PROVEN | No canonical real protected Demo ENTRY evidence |
| Exact-head operational evidence | NOT PROVEN | No real canonical INFRA→SESSION→SUPERVISOR→ARM→ENTRY→HALT→RECOVERY chain |
| Reliability / soak | NOT PROVEN CONNECTED | Multi-week connected Demo soak absent |
| GitHub governance | BLOCKED | `main` protection verified disabled under #103 |
| Live trading readiness | BLOCKED | Strategy, Demo, exact-head evidence, soak, governance and production proof incomplete |

The matrix is diagnostic only; no row grants release authority by itself.

## C2B0 canonical-main evidence

Final exact pre-merge head:

`e9df55204d5127e585166fd364612ab940d114a1`

Qualified merge / main SHA:

`ff684ab85b92151c215c7d5cc85bfc527fecb5eb`

Qualification:

- **10/10** applicable exact-head PR workflows SUCCESS;
- **11/11** post-merge push workflows SUCCESS;
- `canonical-security-regression` run `34056249717` — all job steps SUCCESS on PostgreSQL 16 with v107→v121 migration lineage;
- `release-provenance` run `34056249725` — qualification + signed SLSA + signed SBOM attestation SUCCESS;
- `canonical-deployment-regression` run `34056249729` — real PostgreSQL repository integration, complete regression, coverage, durable DB contracts and fail-closed deployment-state steps SUCCESS;
- exact C2B0 pytest counts are not copied from the older C2A4 SHA because the available connector does not expose stable counts for this run.

Preservation / security:

- frozen v121/001 Git blob `cae1dd432050f235b94d230b2e46c862d38b58c6`;
- runner-side SHA-256 `a03a738d7036c59338ef2ebe085a28a266fd0b440d97ca8e3fae59379efe8e21`;
- final canonical-security workflow SHA-256 `5977182e1ae6fab8da08b5b1addbf7bb23a02af0477332e32f92349cd2ea61dd`;
- reader role = SELECT only;
- writer role = SELECT + INSERT and sequence USAGE only;
- both roles non-owner, no UPDATE/DELETE/TRUNCATE/DDL/mutation-function EXECUTE;
- runtime and bootstrap readiness bind triggers to exact `public` mutation function with `BEFORE` timing;
- wrong-schema same-named trigger and stored hash/event-id tamper fail closed;
- neutral ARM evidence preserves historical READY/read-only/flat/idle/schema/no-order/no-mainnet safety invariants.

C2B0 contains no connected preflight acquisition, broker client, order mutation, strategy/risk-economics change, v122 session-risk, session-start, recovery or mainnet capability.

## Completed canonical foundation

C2A0–C2A4 remain qualified historical components at their exact merge SHAs. C2B0 extends the canonical foundation with the first v121 control-state durability layer. None of these deterministic infrastructure gates proves a profitable strategy or a real broker mutation.

## Current executable gate: C2B1

Issue #130 defines the bounded strategy-free v122 session-risk persistence extraction.

Primary source: PR #84 head `bdcdf7189b56b494e14ac746ad9b867c108dcd30`.

Persistence-only refinement: PR #86 head `700e59e0b67329ea7df00bbd78aebd6ddfdba334` adds `load_active()`; the runtime terminal handoff from PR #86 stays outside C2B1.

Verified frozen v122/001 Git blob at PR #84 and PR #88:

`25f05f93cf11165416e999321ac5d4a422b9a1a3`

Runner-side SHA-256 still must be measured before implementation.

The v122 migration already provides:

- singleton active ledger;
- immutable positive opening equity;
- non-decreasing peak equity;
- CAS revision/checkpoint state;
- active-ledger DELETE/TRUNCATE rejection;
- append-only terminal outcome journal with UPDATE/DELETE/TRUNCATE rejection.

The historical Python domain layer cannot be copied wholesale because it imports `CryptoSessionRiskState` and post-trade accounting types. C2B1 may extract only neutral immutable outcome/ledger records, deterministic codecs/hashes, restart reconstruction and PostgreSQL CAS under an explicit non-owner role.

Explicitly outside C2B1:

- `CryptoSessionRiskState` conversion;
- post-trade accounting-derived builders;
- wallet/equity observation acquisition;
- PR #85 connected session start;
- PR #86 terminal evidence → risk commit → checkpoint ACK runtime bridge;
- broker/order/recovery/mainnet capability.

## Later C2B boundaries

### C2B2 — connected one-time session start

Source semantics: PR #85. Requires canonical GET-only broker observability and fixed-egress read-only account evidence. It is intentionally separate from database-only C2B1.

### C2B3 — terminal evidence → risk commit → checkpoint ACK

Source semantics: PR #86. Preserve crash-safe ordering and idempotent retry only after C2B1 is canonical.

### C2C — active-trade supervision and controlled recovery

Historical sources #87/#88. Recovery may only restore known-safe state; it cannot create a second ENTRY.

## Strategy evidence remains negative

Current frozen Bybit price-only facts remain:

- 102 shared-capital trades;
- 36 WIN / 11 BE / 55 LOSS;
- net approximately `-176.67 USDT` on 1,000 USDT reference equity.

This does not support strategy promotion.

## Current formal blockers

### P0

- `main` branch protection / required review enforcement — #103;
- positive strategy edge — FAIL / not proven;
- real protected Demo entry — not proven;
- exact-head real-broker evidence — not proven;
- independent live approval / tiny-cap pilot — absent.

### P1

- C2B1 strategy-free v122 persistence — #130;
- V107–V109 physical TRUNCATE hardening — #109;
- connected Demo soak;
- production KMS/HSM, backup/restore, external audit, observability and incident evidence.

## Historical evidence archive

The larger pre-C2A0 matrix remains at `docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md`. It remains evidence for the exact revisions it names but cannot override this current matrix.
