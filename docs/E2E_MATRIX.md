# ASTRA Trade Bot — End-to-End Readiness Matrix

This is the **current** human-readable readiness authority. Historical detail that was current before C2A0 remains preserved byte-for-byte in `docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md`.

`CURRENT_SYSTEM_STATUS.json` is the companion machine-readable status. Neither file authorizes Demo or live trading.

## Current system identities — observed 2026-09-06

| Identity | Exact reference | Status | What is actually proven |
|---|---|---|---|
| Qualified product/security main | `043fc3003b055dc1e953854798627048d3f26960` | `ENGINEERING_BASELINE_PASS` | C2A4 merge SHA completed 11/11 post-merge workflows successfully; PostgreSQL/security/release qualification is green; not broker/profit/live proof |
| C2A0 runtime lease | PR #113 | `EXTRACTED_AND_QUALIFIED` | Strategy-free v119 singleton runtime lease; no network/order capability |
| C2A1 runtime-role boundary | PR #116 | `EXTRACTED_AND_QUALIFIED` | Long-running v119 runtime credential is non-owner and least-privilege |
| C2A2 append-only audit | PR #118 | `EXTRACTED_AND_QUALIFIED` | Frozen v120/001 byte-preserved; forward TRUNCATE rejection; SELECT/INSERT-only runtime audit access |
| C2A3 typed persistence | PR #121 | `EXTRACTED_AND_QUALIFIED` | Strategy-free immutable v120 authorization/provenance/terminal records and PostgreSQL adapters |
| C2A4 active-excursion CAS persistence | PR #124 / issue #122 | `EXTRACTED_AND_QUALIFIED` | Neutral v119 active-excursion record plus singleton initialize/load/CAS-save/CAS-clear persistence under the non-owner runtime role |
| Current consolidation gate | C2B0 / issue #125 | `IN_PROGRESS` | Extract v121 append-only ARM/HALT journal persistence only; connected preflight and order guard are explicitly excluded |
| Historical operational source boundary | PR #93 / `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b` | `DECOMPOSITION_SOURCE_DEMO_UNPROVEN` | Preservation/decomposition source only |
| Active research head | PR #100 / `918dbc57c0633c6dc549f1f036d2ae659b289b46` | `RESEARCH_ONLY` | Derivatives-context research incomplete; no strategy-promotion authority |
| Strategy profitability | frozen Bybit price-only evidence | `FAIL / NOT PROVEN` | 102 trades, 36 WIN / 11 BE / 55 LOSS, about `-176.67 USDT` on 1,000 USDT |
| GitHub governance | current server state | `VERIFIED_DISABLED` | `main` protection / required-status enforcement disabled; #103 |
| Live/mainnet | canonical fail-closed flags | `FAIL_CLOSED` | External routing, live trading and mainnet entry disabled |

## Current capability matrix

| Capability | Current state | Evidence / blocker |
|---|---|---|
| Architecture cohesion | STRONG CORE; CONSOLIDATION IN PROGRESS | C2A0–C2A4 are canonical; v121/v122/session/supervisor layers still require bounded extraction |
| Software correctness | STRONG PARTIAL / CURRENT BASELINE PASS | C2A4 PostgreSQL-aware full regression: `1124 passed / 2 dedicated fleet skips`; 11/11 post-merge workflows green |
| Strategy profitability | FAIL / NOT PROVEN | Frozen Bybit replay negative; no positive untouched cost-adjusted OOS edge |
| OMS / execution safety | STRONG DETERMINISTIC | Mature mutation/reconciliation safeguards exist, but connected canonical proof is absent |
| Database runtime security | C2A1–C2A4 QUALIFIED | Non-owner v119 runtime role, v120 physical append-only hardening, typed audit persistence and v119 excursion CAS persistence proven |
| Durable ARM/HALT control journal | C2B0 IN PROGRESS | Historical v121 file mixes journal with connected preflight and order-client guard; only persistence-safe subset may be canonicalized |
| Session-risk durability | BLOCKED ON DECOMPOSITION | Historical v122 ledger imports strategy risk state and post-trade accounting; wholesale copy prohibited |
| Real external integration | BLOCKED | No current credential-backed broker proof sufficient for release |
| Demo execution | NOT PROVEN | No canonical real protected Demo ENTRY evidence |
| Exact-head operational evidence | NOT PROVEN | No real canonical INFRA→SESSION→SUPERVISOR→ARM→ENTRY→HALT→RECOVERY chain |
| Reliability / soak | NOT PROVEN CONNECTED | Multi-week connected Demo soak absent |
| Supply-chain / release provenance | PASS FOR C2A4 MERGE SHA | Signed SLSA provenance and signed SBOM attestation on `043fc300...` |
| GitHub governance | BLOCKED | `main` protection verified disabled under #103 |
| Live trading readiness | BLOCKED | Strategy, Demo, exact-head evidence, soak, governance and production external proof incomplete |

The matrix is diagnostic only; no row can grant a release gate by itself.

## C2A4 canonical-main evidence

Exact C2A4 merge SHA:

`043fc3003b055dc1e953854798627048d3f26960`

Final exact pre-merge head:

`00a0deffa954c70e44fb70483556746363228535`

Qualification:

- **10/10** applicable exact-head pre-merge workflows SUCCESS;
- **11/11** post-merge push workflows SUCCESS on the exact merge SHA;
- `canonical-security-regression` run `34039454929` — PostgreSQL **16.15**, focused suite **282 passed**, full suite **1124 passed / 2 skipped**;
- both skips are the separate fleet-deployment tests requiring `ASTRA_TEST_FLEET_DEPLOYMENT_DSN`; C2A4 PostgreSQL tests executed and did not skip;
- runtime-role `TRUNCATE`, `ALTER TABLE` and `DROP TABLE` attempts against the active-excursion table were rejected;
- `release-provenance` run `34039454985` — qualification PASS, signed SLSA build provenance PASS, signed SBOM attestation PASS;
- canonical-security workflow frozen SHA-256: `8ff9793125804197a828fbec6f56d574882147a62fb73c6201a5cdf6b56823dc`.

C2A4 proves only the strategy-free v119 active-excursion persistence boundary:

1. immutable neutral state/checkpoint records;
2. deterministic canonical JSON and SHA-256 revision identity;
3. singleton `ACTIVE` initialize/load/CAS-save/CAS-clear behavior;
4. stale revision, cross-entry identity and stored-state tamper rejection;
5. operation through the existing C2A1 non-owner runtime role;
6. no runtime migration authority, DDL, TRUNCATE, strategy, broker, market-data, order, ARM/HALT or mainnet capability.

It does **not** prove a broker connection, Demo execution, strategy profitability, exact-head operational evidence, soak, production readiness or live readiness.

## Completed C2A foundation

### C2A0 — durable strategy-free runtime lease

PR #113 canonicalized the byte-preserved v119 singleton runtime lease. Frozen migration SHA-256:

`c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e`.

### C2A1 — non-owner PostgreSQL runtime role

PR #116 separated bootstrap/migration authority from the long-running runtime credential. The runtime role is non-owner and lacks database/schema create, DDL and TRUNCATE authority.

### C2A2 — v120 append-only audit hardening

PR #118 preserved historical v120/001 byte-for-byte and added forward statement-level TRUNCATE rejection. Frozen v120/001 SHA-256:

`613a21fba9142f34141cb7d6c81938b175250fb4dbda9bd37726e5627df094cf`.

### C2A3 — strategy-free v120 typed persistence

PR #121 canonicalized immutable approved-entry authorization, outcome-free entry provenance and fully reconciled terminal diagnostic persistence only. Historical strategy-derived builders remained excluded.

### C2A4 — strategy-free v119 active-excursion CAS persistence

PR #124 / issue #122 is completed and post-merge qualified. Historical tracker/observer/broker/strategy builders were deliberately excluded.

## Current executable gate: C2B0

Issue #125 defines the next bounded unit from historical PR #80 head `f40d9dee0baadd254a6fe7425b117be709088f9d`.

The historical v121 migration is useful, but the historical Python control-plane file is mixed:

- it imports `BybitDemoOrderRequest`;
- it imports connected-preflight types;
- it contains `ControlPlaneGuardedBybitDemoClient.place_market_order`.

C2B0 therefore permits only:

1. neutral immutable ARM/HALT event records;
2. canonical evidence hashing and bounded ARM timestamps as persistence invariants;
3. append-only PostgreSQL v121 journal;
4. fail-closed latest-event rehydration;
5. least-privilege read/append roles;
6. forward physical TRUNCATE hardening if the preservation audit confirms the historical gap.

C2B0 explicitly excludes connected preflight, any broker client, any `place_market_order` guard, strategy/risk economics, v122 session-risk, Demo ENTRY and mainnet routing.

## Why v122 is not being pulled forward

Historical PR #84's `bybit_demo_session_risk_ledger.py` imports both `app.strategy.crypto_session_risk.CryptoSessionRiskState` and post-trade accounting types. The PostgreSQL store also exposes `migrate()`. Therefore v122 requires its own strategy-free decomposition and runtime/bootstrap authority separation before it can become canonical.

Historical PR #85 adds credential-backed read-only session-start checks and must wait for canonical C1 GET-only broker observability. PR #86 adds terminal-evidence → risk-commit → checkpoint-ack sequencing and belongs after a safe v122 persistence layer exists.

## Strategy evidence remains negative

Current frozen Bybit price-only facts:

- 2,394 eligible signals;
- 792 plan-eligible;
- 617 independent first-touch episodes;
- TARGET_FIRST 137 / STOP_FIRST 471 / NEITHER 9;
- LONG target-first 27.13%; SHORT 13.91%;
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

- C2B0 strategy-free v121 control-journal isolation — #125;
- V107–V109 append-only TRUNCATE hardening — #109;
- authoritative derivatives point-in-time evidence;
- connected Demo soak;
- production KMS/HSM, backup/restore, external audit, observability and incident evidence.

## Historical evidence archive

The larger pre-C2A0 matrix remains preserved at `docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md`. It remains historical evidence for the exact revisions it names but cannot override this current matrix.
