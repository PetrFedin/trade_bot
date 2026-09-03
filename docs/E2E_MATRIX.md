# ASTRA Trade Bot — End-to-End Readiness Matrix

This is the **current** human-readable readiness authority. Historical detail that was current before C2A0 is preserved byte-for-byte in `docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md` rather than being mixed with current release claims.

`CURRENT_SYSTEM_STATUS.json` is the companion machine-readable status. Neither file authorizes Demo or live trading.

## Current system identities — observed 2026-09-03

| Identity | Exact reference | Status | What is actually proven |
|---|---|---|---|
| Canonical main | `fd04ad3403b7b840100006dbf1829273d9e5e4bb` | `ENGINEERING_BASELINE_PASS` | Current deterministic/security/PostgreSQL/release qualification is green; not broker/profit/live proof |
| Canonical operational foundation | C2A0 / PR #113 | `EXTRACTED_AND_QUALIFIED` | Strategy-free v119 singleton runtime lease is canonical; no network/order capability |
| Canonical PostgreSQL runtime-role boundary | C2A1 / PR #116 | `EXTRACTED_AND_QUALIFIED` | Long-running v119 runtime credential is non-owner and least-privilege; no TRUNCATE/DDL/schema-create authority |
| C2A2 candidate | issue #117 / PR #118 | `IN_PROGRESS` | Byte-preserved v120 audit schema plus forward TRUNCATE hardening and SELECT/INSERT-only runtime contract are being qualified; not yet canonical main evidence |
| Historical operational source boundary | PR #93 / `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b` | `DECOMPOSITION_SOURCE_DEMO_UNPROVEN` | Preserves later operational controls for bounded extraction; not a release candidate |
| Active research head | PR #100 / `918dbc57c0633c6dc549f1f036d2ae659b289b46` | `RESEARCH_ONLY` | Current derivatives-context research; no strategy-promotion authority |
| Strategy profitability | frozen Bybit price-only evidence | `FAIL / NOT PROVEN` | 102 trades, 36 WIN / 11 BE / 55 LOSS, about `-176.67 USDT` on 1,000 USDT |
| GitHub governance | current server state | `VERIFIED_DISABLED` | `main` protection / required-status enforcement disabled; #103 |
| Live/mainnet | canonical fail-closed flags | `FAIL_CLOSED` | External routing, live trading and mainnet entry disabled |

## Current capability matrix

| Capability | Current state | Evidence / blocker | Score |
|---|---|---|---:|
| Architecture cohesion | STRONG CORE; CONSOLIDATION IN PROGRESS | Stable core plus ancestry-free C2A0/C2A1 extraction; later stack still decomposed into bounded units | 8.2 |
| Software correctness | STRONG PARTIAL / CURRENT BASELINE PASS | Canonical security full PostgreSQL regression `1087 passed / 2 dedicated fleet skips`; current release gates green | 8.6 |
| Data integrity | PARTIAL | Strong validation/research contracts; authoritative current derivatives PIT acquisition incomplete | 6.5 |
| Strategy profitability | FAIL / NOT PROVEN | Frozen Bybit replay negative; no positive untouched cost-adjusted OOS edge | 2.0 |
| Risk management | STRONG DETERMINISTIC | Mature pre-trade/portfolio/fail-closed controls; production policy governance/real connected evidence still incomplete | 8.0 |
| OMS / execution safety | STRONG DETERMINISTIC | Durable mutations, at-most-once safeguards, GET-first ambiguity recovery, fault campaigns | 8.5 |
| C2A0 durable runtime authority | QUALIFIED INFRASTRUCTURE PRIMITIVE | v119 singleton lease, no TTL takeover, exact-owner release, no strategy/network/order capability | 8.0 |
| Database runtime-role security | C2A1 QUALIFIED; C2A2 IN PROGRESS | v119 runtime role is non-owner least-privilege; v120 append-only audit boundary is being hardened in #118 | 7.5 |
| Real external integration | BLOCKED | No current credential-backed broker proof sufficient for release | 4.0 |
| Demo execution | NOT PROVEN | Protected one-shot design exists historically; no canonical real Demo ENTRY evidence | 3.0 |
| Exact-head operational evidence | NOT PROVEN | No real canonical INFRA->SESSION->SUPERVISOR->ARM->ENTRY->HALT->RECOVERY chain | 2.0 |
| Reliability / soak | NOT PROVEN CONNECTED | Deterministic fault evidence exists; multi-week connected Demo soak absent | 4.0 |
| Supply-chain / release provenance | PASS FOR CURRENT MAIN | Strict lock audit, build/release evidence, signed SLSA and SBOM on current main | 8.8 |
| GitHub governance | BLOCKED | `main` protection verified disabled | 3.0 |
| Production infrastructure | PARTIAL / EXTERNAL EVIDENCE MISSING | KMS/HSM, backup/restore, external audit and target-environment proof incomplete | 4.0 |
| Live trading readiness | BLOCKED | Strategy, Demo, exact-head evidence, soak, governance, production external proof and live approval incomplete | 2.0 |
| Live profitability | NOT STARTED / UNPROVEN | No authorized mainnet pilot | 0.0 |

Scores are diagnostic only and cannot grant a gate.

## Current canonical-main evidence

Signed canonical main: `fd04ad3403b7b840100006dbf1829273d9e5e4bb`.

- `canonical-security-regression` run `33733873613` — PASS.
  - PostgreSQL 16 connected inside CI.
  - full PostgreSQL-aware suite: **1087 passed / 2 skipped**.
  - the two skips are the distinct `ASTRA_TEST_FLEET_DEPLOYMENT_DSN` tests owned by the dedicated fleet deployment contour.
  - v119 runtime-role tests prove permission-denied behavior for `TRUNCATE`, `ALTER TABLE`, `DROP TABLE`, `CREATE TABLE` and `CREATE SCHEMA` under the runtime credential.
  - architecture/static audits, stress, durable DB contracts and fail-closed live-state checks PASS.
- `release-provenance` run `33733873591` — PASS.
  - lock freshness PASS;
  - strict dependency audit PASS;
  - full regression PASS;
  - wheel/sdist + release evidence PASS;
  - signed SLSA provenance PASS;
  - signed SBOM attestation PASS.
- `schema99-external-paper-roundtrip` run `33733873614` — PASS.
- `stable-runtime-import-boundary` run `33733873647` — PASS.
- `product-composition` run `33733873576` — PASS.
- `stable-core-quality` run `33733873599` — PASS.
- `release-governance` run `33733873612` — PASS while correctly reporting branch protection as disabled.

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

## C2A2 — current bounded candidate

Issue #117 / PR #118 is the next security-preserving extraction gate.

The historical v120/001 migration is preserved byte-for-byte:

- Git blob: `b337ef19dc7da4a3fcbc0a11a8d6d7d85dff3b00`;
- SHA-256: `613a21fba9142f34141cb7d6c81938b175250fb4dbda9bd37726e5627df094cf`.

C2A2 adds a **forward** migration rather than editing frozen v120/001. Required contract:

- original UPDATE/DELETE append-only triggers remain unchanged;
- all three v120 audit tables gain physical statement-level `BEFORE TRUNCATE` rejection;
- long-running runtime role gets exactly `SELECT, INSERT` on each v120 audit table;
- no runtime UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER/DDL/schema-create authority;
- v119 C2A1 least-privilege contract remains unchanged;
- canonical-security CI explicitly triggers on v119/v120 security files and applies their migrations in PostgreSQL 16;
- typed v120 persistence adapters remain outside this gate until their pure domain dependency closure is separately audited.

C2A2 remains `IN_PROGRESS` until the final exact PR head and post-merge canonical main are qualified. It is not broker or trading evidence.

Parent security issue: #107. Canonical V107–V109 append-only TRUNCATE hardening is separately tracked by #109.

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

Historical PR #89 defines important future C3 semantics that must be preserved when canonicalized: fixed egress, existing ARM, short-lived explicit approval, immutable authorization/provenance, `SUBMIT_STARTED` before broker mutation, at most one ENTRY attempt, no blind resubmit, mandatory reconciliation, protection/reduce-only recovery, no second ENTRY from recovery, no auto-ARM and no mainnet write path.

Historical PR #90 defines the future C4 evidence order:

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
- C2A2 v120 append-only TRUNCATE/least-privilege hardening — #117/#118/#107;
- V107–V109 append-only TRUNCATE hardening — #109;
- authoritative derivatives PIT evidence;
- connected Demo soak;
- production KMS/HSM, backup/restore, external audit, observability/incident evidence.

## Historical evidence archive

The previously current, much larger E2E matrix is preserved byte-for-byte at:

`docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md`

That snapshot remains valid historical evidence for the exact heads/runs it names, but it is not allowed to override this current matrix.
