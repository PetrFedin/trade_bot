# ASTRA Trade Bot — End-to-End Readiness Matrix

This is the **current** human-readable readiness authority. Historical detail that was current before C2A0 is preserved byte-for-byte in `docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md` rather than being mixed with current release claims.

`CURRENT_SYSTEM_STATUS.json` is the companion machine-readable status. Neither file authorizes Demo or live trading.

## Current system identities — observed 2026-09-03

| Identity | Exact reference | Status | What is actually proven |
|---|---|---|---|
| Qualified product/security main | `319fb407f1b99ad76a25a04adb96b84c171b6275` | `ENGINEERING_BASELINE_PASS` | C2A2 merge SHA completed 11/11 post-merge workflows successfully; deterministic/security/PostgreSQL/release qualification is green; not broker/profit/live proof |
| Canonical operational foundation | C2A0 / PR #113 | `EXTRACTED_AND_QUALIFIED` | Strategy-free v119 singleton runtime lease is canonical; no network/order capability |
| Canonical PostgreSQL runtime-role boundary | C2A1 / PR #116 | `EXTRACTED_AND_QUALIFIED` | Long-running v119 runtime credential is non-owner and least-privilege; no TRUNCATE/DDL/schema-create authority |
| Canonical v120 append-only audit boundary | C2A2 / PR #118 | `EXTRACTED_AND_QUALIFIED` | Frozen v120/001 is byte-preserved; forward physical TRUNCATE rejection and SELECT/INSERT-only runtime access are proven on PostgreSQL 16 |
| Current consolidation candidate | C2A3 / issue #119 | `IN_PROGRESS` | Persistence-safe v120 record contracts must be separated from historical strategy/broker/accounting builders before typed stores can be canonicalized |
| Historical operational source boundary | PR #93 / `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b` | `DECOMPOSITION_SOURCE_DEMO_UNPROVEN` | Preserves later operational controls for bounded extraction; not a release candidate |
| Active research head | PR #100 / `918dbc57c0633c6dc549f1f036d2ae659b289b46` | `RESEARCH_ONLY` | Current derivatives-context research; no strategy-promotion authority |
| Strategy profitability | frozen Bybit price-only evidence | `FAIL / NOT PROVEN` | 102 trades, 36 WIN / 11 BE / 55 LOSS, about `-176.67 USDT` on 1,000 USDT |
| GitHub governance | current server state | `VERIFIED_DISABLED` | `main` protection / required-status enforcement disabled; #103 |
| Live/mainnet | canonical fail-closed flags | `FAIL_CLOSED` | External routing, live trading and mainnet entry disabled |

`319fb407...` is the exact C2A2 product/security evidence subject. Later documentation-only commits do not by themselves promote trading capability and must not be confused with a new broker or strategy qualification.

## Current capability matrix

| Capability | Current state | Evidence / blocker | Score |
|---|---|---|---:|
| Architecture cohesion | STRONG CORE; CONSOLIDATION IN PROGRESS | Stable core plus ancestry-free C2A0/C2A1/C2A2 extraction; later typed persistence/control stack still being decomposed | 8.3 |
| Software correctness | STRONG PARTIAL / CURRENT BASELINE PASS | Canonical security full PostgreSQL regression `1099 passed / 2 dedicated fleet skips`; C2A2 post-merge release gates green | 8.7 |
| Data integrity | PARTIAL | Strong validation/research contracts; authoritative current derivatives PIT acquisition incomplete | 6.5 |
| Strategy profitability | FAIL / NOT PROVEN | Frozen Bybit replay negative; no positive untouched cost-adjusted OOS edge | 2.0 |
| Risk management | STRONG DETERMINISTIC | Mature pre-trade/portfolio/fail-closed controls; production policy governance/real connected evidence still incomplete | 8.0 |
| OMS / execution safety | STRONG DETERMINISTIC | Durable mutations, at-most-once safeguards, GET-first ambiguity recovery, fault campaigns | 8.5 |
| C2A0 durable runtime authority | QUALIFIED INFRASTRUCTURE PRIMITIVE | v119 singleton lease, no TTL takeover, exact-owner release, no strategy/network/order capability | 8.0 |
| Database runtime-role security | C2A1 + C2A2 QUALIFIED | v119 non-owner runtime role plus v120 physical append-only TRUNCATE hardening and exact SELECT/INSERT audit-table privileges | 8.2 |
| Real external integration | BLOCKED | No current credential-backed broker proof sufficient for release | 4.0 |
| Demo execution | NOT PROVEN | Protected one-shot design exists historically; no canonical real Demo ENTRY evidence | 3.0 |
| Exact-head operational evidence | NOT PROVEN | No real canonical INFRA->SESSION->SUPERVISOR->ARM->ENTRY->HALT->RECOVERY chain | 2.0 |
| Reliability / soak | NOT PROVEN CONNECTED | Deterministic fault evidence exists; multi-week connected Demo soak absent | 4.0 |
| Supply-chain / release provenance | PASS FOR C2A2 MERGE SHA | Strict lock audit, build/release evidence, signed SLSA and SBOM on `319fb407...` | 8.8 |
| GitHub governance | BLOCKED | `main` protection verified disabled | 3.0 |
| Production infrastructure | PARTIAL / EXTERNAL EVIDENCE MISSING | KMS/HSM, backup/restore, external audit and target-environment proof incomplete | 4.0 |
| Live trading readiness | BLOCKED | Strategy, Demo, exact-head evidence, soak, governance, production external proof and live approval incomplete | 2.0 |
| Live profitability | NOT STARTED / UNPROVEN | No authorized mainnet pilot | 0.0 |

Scores are diagnostic only and cannot grant a gate.

## C2A2 canonical-main evidence

Exact C2A2 merge SHA: `319fb407f1b99ad76a25a04adb96b84c171b6275`.

All **11/11** post-merge workflows completed with `success`; there were no failed, cancelled or skipped workflow conclusions in the exact-SHA completed-run set.

Key runs:

- `canonical-security-regression` run `33783216938` — PASS.
  - PostgreSQL 16 connected inside CI.
  - v107 -> v108 -> v109 -> v119 -> v120/001 -> v120/002 migrations applied repeatedly in lineage order.
  - focused v107-v120 security tests PASS.
  - full PostgreSQL-aware suite: **1099 passed / 2 skipped**.
  - the two skips are the distinct `ASTRA_TEST_FLEET_DEPLOYMENT_DSN` tests owned by the dedicated fleet-deployment contour.
  - C2A1 runtime-role prohibitions remain effective.
  - v120 owner-level UPDATE/DELETE/TRUNCATE rejection is proven.
  - v120 runtime SELECT/INSERT works while UPDATE/DELETE/TRUNCATE/ALTER/DROP/CREATE are rejected.
  - branch-aware coverage floors, architecture/static audits, stress, durable DB contracts and fail-closed live-state checks PASS.
- `release-provenance` run `33783216901` — PASS.
  - exact hash-locked install PASS;
  - lock freshness PASS;
  - strict dependency audit PASS;
  - full regression PASS;
  - wheel/sdist + release evidence PASS;
  - signed SLSA provenance PASS;
  - signed SBOM attestation PASS.
- `schema99-external-paper-roundtrip` run `33783216962` — PASS.
- `stable-runtime-import-boundary` run `33783216985` — PASS.
- `product-composition` run `33783216945` — PASS.
- `stable-core-quality` run `33783216906` — PASS.
- `release-governance` run `33783216921` — PASS while correctly reporting branch protection as disabled.
- `canonical-deployment-regression` run `33783216950` — PASS.
- `ci-action-supply-chain-policy` run `33783216972` — PASS.
- `compatibility-release-audits` run `33783217121` — PASS.
- `schema109-remote-signer-attestation` run `33783216907` — PASS.

## C2A0 — completed canonical extraction

PR #113 extracted the audited v119 runtime-lease primitive onto canonical main without inheriting stacked research/operational ancestry:

- pure v119 runtime-lease domain;
- PostgreSQL lease adapter;
- frozen v119 migration;
- focused unit tests;
- real PostgreSQL tests.

Frozen migration SHA-256:
`c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e`.

Proven invariants:

- singleton durable owner;
- concurrent acquire yields one owner;
- exact-owner release;
- wrong-owner release rejected;
- no TTL/age-based stale takeover;
- unsafe stale-takeover/mainnet flags rejected by PostgreSQL constraints;
- no strategy/research/network client imports;
- no order submit/cancel/replace/protection capability.

## C2A1 — completed PostgreSQL runtime-role hardening

PR #116 separated bootstrap/migration authority from the long-running v119 Demo runtime credential.

Proven on PostgreSQL 16:

- runtime role is a distinct non-owner role;
- no superuser / createdb / createrole / replication / bypass-RLS authority;
- no role memberships;
- no database `CREATE`;
- `public` schema `USAGE` only, no schema `CREATE`;
- runtime cannot own canonical v119 tables;
- runtime lease privileges are exactly `SELECT, INSERT, DELETE`;
- runtime excursion privileges are exactly `SELECT, INSERT, UPDATE, DELETE`;
- no effective `TRUNCATE`, `REFERENCES`, `TRIGGER` or DDL authority;
- read-only preflight binds the actual connected credential to the expected runtime role;
- over-privilege fails closed and reconciliation removes direct privilege drift;
- no strategy, broker network, order-write, ARM, approval, Demo activation or mainnet behavior was added.

C2A1 does **not** prove broker connectivity, Demo execution, strategy edge, exact-head operational evidence, soak or production readiness.

## C2A2 — completed v120 append-only audit hardening

Issue #117 / PR #118 is now a completed canonical schema/security gate.

Preservation proof:

- pre-merge exact head: `8a7b71e0f286e17d9a79307fc643ed805bb1f5ae`;
- merge SHA: `319fb407f1b99ad76a25a04adb96b84c171b6275`;
- frozen v120/001 Git blob: `b337ef19dc7da4a3fcbc0a11a8d6d7d85dff3b00`;
- frozen v120/001 SHA-256: `613a21fba9142f34141cb7d6c81938b175250fb4dbda9bd37726e5627df094cf`.

The historical v120/001 migration was not edited. A forward `v120/002` migration adds statement-level `BEFORE TRUNCATE` rejection to all three v120 audit tables while preserving the original UPDATE/DELETE triggers.

Proven runtime boundary:

- C2A1/v119 least-privilege policy remains the base policy;
- runtime receives exactly `SELECT, INSERT` on all three v120 audit tables;
- runtime receives no UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER/DDL/schema-create authority;
- runtime cannot own v120 audit tables or inherit an owner role;
- direct mutation-function execution is revoked;
- privilege drift is reconciled back to the exact policy;
- missing TRUNCATE hardening fails closed;
- PostgreSQL 16 owner-level UPDATE/DELETE/TRUNCATE protection is physically exercised;
- typed v120 persistence adapters were deliberately not promoted.

C2A2 does **not** prove broker connectivity, Demo execution, strategy edge, exact-head operational evidence, soak, production readiness or live readiness.

Canonical V107-V109 event-table TRUNCATE hardening remains a separate issue #109 and is not falsely closed by C2A2.

## C2A3 — current bounded extraction gate

Issue #119 is the next consolidation gate.

The historical v120 PostgreSQL stores are simple immutable INSERT/SELECT adapters, but their old record/build modules are not persistence-pure:

- approved-entry authorization builder reaches operator approval, strategy selector, market data and crypto strategy types;
- entry-provenance builder reaches cycle status, ranked fallback, strategy selector and crypto strategy types;
- terminal-evidence builder reaches excursion tracking, post-trade accounting and crypto strategy types.

Therefore those stores may not be copied wholesale into canonical main.

C2A3 must first separate:

1. **Persistence-safe immutable record contracts** — pure data/value objects, canonical encoding/hash and validation.
2. **Trading-derived builders** — strategy/broker/cycle/accounting construction logic, which remains outside C2A3 until separately audited.

Required C2A3 invariants:

- exact historical file/blob preservation audit before extraction;
- import classification for every dependency;
- no strategy/broker/market-data/order capability in persistence contracts;
- no future/outcome data in pre-submit authorization/provenance records;
- deterministic canonical hashing and tamper detection;
- idempotent INSERT / fail-closed identity conflict;
- SELECT rehydration validates content/hash;
- no UPDATE/DELETE/TRUNCATE path;
- if typed stores are promoted, they operate only through the already-qualified C2A2 runtime role on PostgreSQL 16;
- terminal evidence cannot authorize or retune future entries.

C2A3 remains `IN_PROGRESS`. No typed v120 persistence adapter is canonical yet.

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

This does not support strategy promotion. The incomplete derivatives-context experiment must first obtain authoritative point-in-time evidence and then survive a newly frozen validation protocol.

## Operational release boundary

Historical PR #89 defines important future operational semantics that must be preserved when canonicalized: fixed egress, existing ARM, short-lived explicit approval, immutable authorization/provenance, `SUBMIT_STARTED` before broker mutation, at most one ENTRY attempt, no blind resubmit, mandatory reconciliation, protection/reduce-only recovery, no second ENTRY from recovery, no auto-ARM and no mainnet write path.

Historical PR #90 defines the future evidence order:

```text
INFRA_READY
-> SESSION_READY
-> SUPERVISOR_READY
-> ARM_PROVEN
-> DEMO_ENTRY_PROVEN
-> HALT_PROVEN
-> RECOVERY_DRILL_PROVEN
```

Neither historical design is current real-broker evidence.

## Current formal blockers

P0:
- branch protection / required review enforcement — #103;
- strategy edge — FAIL / not proven;
- real protected Demo entry — not proven;
- exact-head real-broker evidence — not proven;
- independent live approval / tiny-cap pilot — absent.

P1:
- C2A3 strategy-free v120 persistence-contract isolation — #119;
- V107-V109 append-only TRUNCATE hardening — #109;
- authoritative derivatives PIT evidence;
- connected Demo soak;
- production KMS/HSM, backup/restore, external audit, observability/incident evidence.

## Historical evidence archive

The previously current, much larger E2E matrix is preserved byte-for-byte at:

`docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md`

That snapshot remains valid historical evidence for the exact heads/runs it names, but it is not allowed to override this current matrix.
