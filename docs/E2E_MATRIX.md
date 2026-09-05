# ASTRA Trade Bot — End-to-End Readiness Matrix

This is the **current** human-readable readiness authority. Historical detail that was current before C2A0 is preserved byte-for-byte in `docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md` rather than being mixed with current release claims.

`CURRENT_SYSTEM_STATUS.json` is the companion machine-readable status. Neither file authorizes Demo or live trading.

## Current system identities — observed 2026-09-05

| Identity | Exact reference | Status | What is actually proven |
|---|---|---|---|
| Qualified product/security main | `090f34b11a877ce24f8a15a74b296e287aae3918` | `ENGINEERING_BASELINE_PASS` | C2A3 merge SHA completed 11/11 post-merge workflows successfully; PostgreSQL/security/release qualification is green; not broker/profit/live proof |
| Canonical operational foundation | C2A0 / PR #113 | `EXTRACTED_AND_QUALIFIED` | Strategy-free v119 singleton runtime lease is canonical; no network/order capability |
| Canonical PostgreSQL runtime-role boundary | C2A1 / PR #116 | `EXTRACTED_AND_QUALIFIED` | Long-running v119 runtime credential is non-owner and least-privilege; no TRUNCATE/DDL/schema-create authority |
| Canonical v120 append-only audit boundary | C2A2 / PR #118 | `EXTRACTED_AND_QUALIFIED` | Frozen v120/001 is byte-preserved; forward physical TRUNCATE rejection and SELECT/INSERT-only runtime access are proven on PostgreSQL 16 |
| Canonical v120 typed persistence | C2A3 / PR #121 | `EXTRACTED_AND_QUALIFIED` | Immutable strategy-free authorization/provenance/terminal records and SELECT/INSERT PostgreSQL adapters are canonical; historical trading-derived builders remain excluded |
| Current consolidation candidate | C2A4 / issue #122 | `IN_PROGRESS` | Extract neutral v119 active-excursion checkpoint/CAS persistence without strategy, broker, market-data or monitor ancestry |
| Historical operational source boundary | PR #93 / `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b` | `DECOMPOSITION_SOURCE_DEMO_UNPROVEN` | Preserves later operational controls for bounded extraction; not a release candidate |
| Active research head | PR #100 / `918dbc57c0633c6dc549f1f036d2ae659b289b46` | `RESEARCH_ONLY` | Derivatives-context research remains incomplete and has no strategy-promotion authority |
| Strategy profitability | frozen Bybit price-only evidence | `FAIL / NOT PROVEN` | 102 trades, 36 WIN / 11 BE / 55 LOSS, about `-176.67 USDT` on 1,000 USDT |
| GitHub governance | current server state | `VERIFIED_DISABLED` | `main` protection / required-status enforcement disabled; #103 |
| Live/mainnet | canonical fail-closed flags | `FAIL_CLOSED` | External routing, live trading and mainnet entry disabled |

`090f34b1...` is the exact C2A3 product/security evidence subject. Subsequent documentation-only commits do not by themselves promote trading capability and must not be confused with broker or strategy qualification.

## Current capability matrix

| Capability | Current state | Evidence / blocker |
|---|---|---|
| Architecture cohesion | STRONG CORE; CONSOLIDATION IN PROGRESS | C2A0–C2A3 ancestry-free foundations are canonical; active-excursion/control/session/supervisor layers remain bounded extraction work |
| Software correctness | STRONG PARTIAL / CURRENT BASELINE PASS | C2A3 PostgreSQL-aware full regression: `1112 passed / 2 dedicated fleet skips`; post-merge release gates green |
| Data integrity | PARTIAL | Strong validation/research contracts; authoritative derivatives PIT acquisition incomplete |
| Strategy profitability | FAIL / NOT PROVEN | Frozen Bybit replay negative; no positive untouched cost-adjusted OOS edge |
| Risk management | STRONG DETERMINISTIC | Mature deterministic pre-trade/portfolio controls; real connected and production governance evidence still incomplete |
| OMS / execution safety | STRONG DETERMINISTIC | Durable mutations, at-most-once safeguards, GET-first ambiguity recovery and fault campaigns exist |
| Database runtime security | C2A1–C2A3 QUALIFIED | v119 non-owner role, v120 append-only TRUNCATE hardening and strategy-free typed audit persistence proven on PostgreSQL 16 |
| Active-excursion persistence | C2A4 IN PROGRESS | Historical CAS semantics are useful, but the old state/builders are coupled to strategy/broker/market-data types |
| Real external integration | BLOCKED | No current credential-backed broker proof sufficient for release |
| Demo execution | NOT PROVEN | Protected one-shot design exists historically; no canonical real Demo ENTRY evidence |
| Exact-head operational evidence | NOT PROVEN | No real canonical INFRA→SESSION→SUPERVISOR→ARM→ENTRY→HALT→RECOVERY chain |
| Reliability / soak | NOT PROVEN CONNECTED | Deterministic fault evidence exists; multi-week connected Demo soak absent |
| Supply-chain / release provenance | PASS FOR C2A3 MERGE SHA | Strict lock audit, build/release evidence, signed SLSA and SBOM on `090f34b1...` |
| GitHub governance | BLOCKED | `main` protection verified disabled |
| Production infrastructure | PARTIAL / EXTERNAL EVIDENCE MISSING | KMS/HSM, backup/restore, external audit and target-environment proof incomplete |
| Live trading readiness | BLOCKED | Strategy, Demo, exact-head evidence, soak, governance and production external proof incomplete |
| Live profitability | NOT STARTED / UNPROVEN | No authorized mainnet pilot |

The matrix is diagnostic only; no row can grant a release gate by itself.

## C2A3 canonical-main evidence

Exact C2A3 merge SHA: `090f34b11a877ce24f8a15a74b296e287aae3918`.

All **11/11** applicable post-merge push workflows completed with `success`; commit check-runs were 12/12 success because `release-provenance` has separate qualify and attest jobs. No failed, cancelled or skipped check-run conclusion was present on the exact merge SHA.

Key evidence:

- `canonical-security-regression` run `33988217105` — PASS on PostgreSQL 16.
  - v107 → v108 → v109 → v119 → v120/001 → v120/002 migration lineage applied twice.
  - C2A3 Ruff and Bandit checks PASS.
  - focused security/persistence suite: **271 passed**.
  - full PostgreSQL-aware suite: **1112 passed / 2 skipped**.
  - the two skips are the distinct fleet-deployment tests requiring `ASTRA_TEST_FLEET_DEPLOYMENT_DSN`.
  - branch-aware coverage floors PASS: V107 93.900804%, V108 98.253968%, V109 98.233216%.
  - durable database contracts, stress, frozen release lineage and fail-closed production-state checks PASS.
- `release-provenance` run `33988217099` — PASS.
  - exact hash-locked dependency graph and lock freshness PASS;
  - dependency audit and full regression PASS;
  - wheel/sdist and release evidence PASS;
  - signed SLSA provenance PASS;
  - signed SBOM attestation PASS.
- `canonical-deployment-regression` run `33988217134` — PASS.
- `stable-runtime-import-boundary` run `33988217118` — PASS.
- `schema99-external-paper-roundtrip` run `33988217077` — PASS.
- `product-composition` run `33988217110` — PASS.
- `stable-core-quality` run `33988217080` — PASS.
- `release-governance` run `33988217102` — PASS while correctly preserving the branch-protection blocker.
- `ci-action-supply-chain-policy` run `33988217100` — PASS.
- `compatibility-release-audits` run `33988217089` — PASS.
- `schema109-remote-signer-attestation` run `33988217103` — PASS.

## Completed C2A foundation

### C2A0 — durable strategy-free runtime lease

PR #113 canonicalized the byte-preserved v119 singleton runtime lease. It proves single-writer ownership, exact-owner release, no TTL/stale takeover and no strategy/network/order capability. Frozen migration SHA-256:

`c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e`.

### C2A1 — non-owner PostgreSQL runtime role

PR #116 separated bootstrap/migration authority from the long-running runtime credential. The runtime role is non-owner, lacks database/schema create and DDL/TRUNCATE authority, and has only the DML required by v119 lease/excursion tables.

### C2A2 — v120 append-only audit hardening

PR #118 preserved historical v120/001 byte-for-byte and added forward statement-level TRUNCATE rejection. Runtime v120 audit-table privileges are exactly `SELECT, INSERT`; UPDATE/DELETE/TRUNCATE/DDL remain unavailable.

Frozen v120/001 evidence:

- Git blob `b337ef19dc7da4a3fcbc0a11a8d6d7d85dff3b00`;
- SHA-256 `613a21fba9142f34141cb7d6c81938b175250fb4dbda9bd37726e5627df094cf`.

### C2A3 — strategy-free v120 typed persistence

Issue #119 / PR #121 is completed and post-merge qualified.

Canonicalized:

- immutable approved-entry authorization record contract;
- immutable outcome-free entry-provenance record contract;
- immutable fully reconciled terminal diagnostic record contract;
- deterministic canonical JSON and SHA-256 validation;
- exact-key/unknown-field fail-closed decoding;
- PostgreSQL SELECT/INSERT stores running through the existing C2A2 runtime-role boundary;
- idempotent re-insert and exact identity conflict detection;
- tamper/checksum and safety-marker validation.

Explicitly not canonicalized in C2A3:

- strategy selector builders;
- crypto strategy types;
- broker/market-data clients;
- cycle/fallback decision logic;
- post-trade behavior that could influence future entries;
- order submission, ARM/HALT or mainnet capability.

C2A3 therefore proves persistence infrastructure, **not** a broker-qualified execution path.

## C2A4 — current bounded extraction gate

Issue #122 is now the next C2A unit.

Historical PR #76 contains a valuable v119 active-excursion checkpoint/CAS store, but the old supporting files mix persistence with `CryptoSide`, `CryptoTradePlan`, broker position/monitor types and Bybit market-data quote types. Wholesale copy is prohibited.

C2A4 must extract only:

1. a neutral immutable active-excursion persistence record;
2. deterministic canonical encoding and checkpoint revision;
3. PostgreSQL singleton initialize/load/CAS-save/CAS-clear behavior;
4. stale-revision and cross-entry rejection;
5. the already-qualified C2A1 least-privilege DML boundary.

It must not import strategy, broker, market-data or monitor builders; must expose no `migrate()`/DDL path; and must remain incapable of order mutation, ARM/HALT, Demo activation or mainnet routing.

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

This does not support strategy promotion. The derivatives-context experiment remains incomplete until authoritative point-in-time acquisition and a newly frozen validation protocol are completed.

## Operational release boundary

Historical PR #89 remains a design source only. A future canonical operational bridge must preserve fixed egress, existing ARM, short-lived explicit approval, immutable authorization/provenance, `SUBMIT_STARTED` before broker mutation, at most one risk-adding ENTRY attempt, no blind resubmit, mandatory reconciliation, protection/reduce-only recovery, no second ENTRY from recovery, no auto-ARM and no mainnet write path.

Historical PR #90 defines the future evidence order:

```text
INFRA_READY
→ SESSION_READY
→ SUPERVISOR_READY
→ ARM_PROVEN
→ DEMO_ENTRY_PROVEN
→ HALT_PROVEN
→ RECOVERY_DRILL_PROVEN
```

Neither historical design is current real-broker evidence.

## Current formal blockers

### P0

- branch protection / required review enforcement — #103;
- positive strategy edge — FAIL / not proven;
- real protected Demo entry — not proven;
- exact-head real-broker evidence — not proven;
- independent live approval / tiny-cap pilot — absent.

### P1

- C2A4 strategy-free active-excursion CAS isolation — #122;
- V107–V109 append-only TRUNCATE hardening — #109;
- authoritative derivatives PIT evidence;
- connected Demo soak;
- production KMS/HSM, backup/restore, external audit, observability and incident evidence.

## Historical evidence archive

The previously current, much larger E2E matrix remains preserved byte-for-byte at:

`docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md`

That snapshot remains valid historical evidence for the exact heads/runs it names, but it cannot override this current matrix.
