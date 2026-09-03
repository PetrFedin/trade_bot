# ASTRA Trade Bot — End-to-End Readiness Matrix

This is the **current** human-readable readiness authority. Historical detail that was current before C2A0 is preserved byte-for-byte in `docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md` rather than being mixed with current release claims.

`CURRENT_SYSTEM_STATUS.json` is the companion machine-readable status. Neither file authorizes Demo or live trading.

## Current system identities — observed 2026-09-03

| Identity | Exact reference | Status | What is actually proven |
|---|---|---|---|
| Canonical main | `e110a4c02f5bf9b9937ff3fbf7e942859be9050d` | `ENGINEERING_BASELINE_PASS` | Current deterministic/security/PostgreSQL/release qualification is green; not broker/profit/live proof |
| Canonical operational foundation | C2A0 / PR #113 | `EXTRACTED_AND_QUALIFIED` | Strategy-free v119 singleton runtime lease is canonical; no network/order capability |
| Historical operational source boundary | PR #93 / `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b` | `DECOMPOSITION_SOURCE_DEMO_UNPROVEN` | Preserves later operational controls for bounded extraction; not a release candidate |
| Active research head | PR #100 / `918dbc57c0633c6dc549f1f036d2ae659b289b46` | `RESEARCH_ONLY` | Current derivatives-context research; no strategy-promotion authority |
| Strategy profitability | frozen Bybit price-only evidence | `FAIL / NOT PROVEN` | 102 trades, 36 WIN / 11 BE / 55 LOSS, about `-176.67 USDT` on 1,000 USDT |
| GitHub governance | current server state | `VERIFIED_DISABLED` | `main` protection / required-status enforcement disabled; #103 |
| Live/mainnet | canonical fail-closed flags | `FAIL_CLOSED` | External routing, live trading and mainnet entry disabled |

## Current capability matrix

| Capability | Current state | Evidence / blocker | Score |
|---|---|---|---:|
| Architecture cohesion | STRONG CORE; CONSOLIDATION IN PROGRESS | Stable core plus first ancestry-free operational extraction C2A0; later stack still being decomposed | 8.1 |
| Software correctness | STRONG PARTIAL / CURRENT BASELINE PASS | Canonical security full PostgreSQL regression `1078 passed / 2 dedicated fleet skips`; current release gates green | 8.5 |
| Data integrity | PARTIAL | Strong validation/research contracts; authoritative current derivatives PIT acquisition incomplete | 6.5 |
| Strategy profitability | FAIL / NOT PROVEN | Frozen Bybit replay negative; no positive untouched cost-adjusted OOS edge | 2.0 |
| Risk management | STRONG DETERMINISTIC | Mature pre-trade/portfolio/fail-closed controls; production policy governance/real connected evidence still incomplete | 8.0 |
| OMS / execution safety | STRONG DETERMINISTIC | Durable mutations, at-most-once safeguards, GET-first ambiguity recovery, fault campaigns | 8.5 |
| C2A0 durable runtime authority | QUALIFIED INFRASTRUCTURE PRIMITIVE | v119 singleton lease, no TTL takeover, exact-owner release, no strategy/network/order capability | 8.0 |
| Database runtime-role security | IN PROGRESS | C2A1/#114 must prove non-owner least privilege and no TRUNCATE/DDL for runtime role | 5.0 |
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

Signed canonical main: `e110a4c02f5bf9b9937ff3fbf7e942859be9050d`.

- `canonical-security-regression` run `33731847837` — PASS.
  - PostgreSQL 16 connected inside CI.
  - V107->V109 migration lineage applied twice.
  - focused lineage regression: `233 passed`.
  - full PostgreSQL-aware suite: **1078 passed / 2 skipped**.
  - the two skips are the distinct `ASTRA_TEST_FLEET_DEPLOYMENT_DSN` tests owned by the dedicated fleet deployment contour.
  - branch-aware coverage: V107 `93.900804%`, V108 `98.253968%`, V109 `98.233216%`.
  - architecture/static audits, stress, durable DB contracts and fail-closed live-state checks PASS.
- `release-provenance` run `33731847749` — PASS.
  - lock freshness PASS;
  - strict dependency audit PASS;
  - full regression PASS;
  - wheel/sdist + release evidence PASS;
  - signed SLSA provenance PASS;
  - signed SBOM attestation PASS.
- `schema99-external-paper-roundtrip` run `33731847806` — PASS.
- `stable-runtime-import-boundary` run `33731847773` — PASS.
- `product-composition` run `33731847986` — PASS.
- `stable-core-quality` run `33731847745` — PASS.
- `release-governance` run `33731847853` — PASS while correctly reporting branch protection as disabled.

## C2A0 — completed canonical extraction

PR #113 extracted exactly five audited blobs from historical PR #110 onto repaired canonical main without inheriting stacked research/operational ancestry:

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

Not proven by C2A0:

- broker connectivity;
- ARM/approval;
- Demo entry;
- protection lifecycle;
- exact-head broker evidence;
- strategy edge;
- production/live readiness.

## Next canonical gate — C2A1 / #114

Before later v120 durable approval/provenance/terminal stores are extracted, canonical v119 must run under a **non-owner least-privilege PostgreSQL role**.

Required proof includes:

- migration/bootstrap authority distinct from runtime authority;
- runtime role not table/schema owner;
- exact DML only;
- no `TRUNCATE`;
- no `ALTER`/`DROP` ownership path;
- no schema `CREATE`;
- fail-closed preflight on overprivilege;
- canonical lease behavior unchanged under that role.

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
- C2A1 least-privilege runtime DB role — #114/#107;
- v120 append-only TRUNCATE/owner-role hardening — #107;
- V107–V109 append-only TRUNCATE hardening — #109;
- authoritative derivatives PIT evidence;
- connected Demo soak;
- production KMS/HSM, backup/restore, external audit, observability/incident evidence.

## Historical evidence archive

The previously current, much larger E2E matrix is preserved byte-for-byte at:

`docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md`

That snapshot remains valid historical evidence for the exact heads/runs it names, but it is not allowed to override this current matrix.
