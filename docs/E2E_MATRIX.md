# ASTRA Trade Bot — End-to-End Readiness Matrix

This is the **current** human-readable readiness authority. Historical detail that was current before C2A0 is preserved byte-for-byte in `docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md` rather than being mixed with current release claims.

`CURRENT_SYSTEM_STATUS.json` is the companion machine-readable status. Neither file authorizes Demo or live trading.

## Current system identities — observed 2026-09-06

| Identity | Exact reference | Status | What is actually proven |
|---|---|---|---|
| Qualified product/security main | `043fc3003b055dc1e953854798627048d3f26960` | `ENGINEERING_BASELINE_PASS` | C2A4 merge SHA completed 11/11 post-merge workflows successfully; PostgreSQL/security/release qualification is green; not broker/profit/live proof |
| Canonical operational foundation | C2A0 / PR #113 | `EXTRACTED_AND_QUALIFIED` | Strategy-free v119 singleton runtime lease is canonical; no network/order capability |
| Canonical PostgreSQL runtime-role boundary | C2A1 / PR #116 | `EXTRACTED_AND_QUALIFIED` | Long-running v119 runtime credential is non-owner and least-privilege; no TRUNCATE/DDL/schema-create authority |
| Canonical v120 append-only audit boundary | C2A2 / PR #118 | `EXTRACTED_AND_QUALIFIED` | Frozen v120/001 is byte-preserved; forward physical TRUNCATE rejection and SELECT/INSERT-only runtime access are proven on PostgreSQL 16 |
| Canonical v120 typed persistence | C2A3 / PR #121 | `EXTRACTED_AND_QUALIFIED` | Immutable strategy-free authorization/provenance/terminal records and SELECT/INSERT PostgreSQL adapters are canonical; historical trading-derived builders remain excluded |
| Canonical v119 active-excursion persistence | C2A4 / PR #124 | `EXTRACTED_AND_QUALIFIED` | Neutral immutable excursion state plus singleton initialize/load/CAS-save/CAS-clear is canonical under the non-owner v119 runtime role; no strategy/broker/market-data/order capability |
| Current consolidation candidate | C2B0 / issue #128 | `AUDIT_IN_PROGRESS` | Canonicalize only the v121 ARM/HALT append-only journal and fail-closed control-state evaluator, excluding connected preflight and order-client guard |
| Historical operational source boundary | PR #93 / `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b` | `DECOMPOSITION_SOURCE_DEMO_UNPROVEN` | Preserves later operational controls for bounded extraction; not a release candidate |
| Active research head | PR #100 / `918dbc57c0633c6dc549f1f036d2ae659b289b46` | `RESEARCH_ONLY` | Derivatives-context research remains incomplete and has no strategy-promotion authority |
| Strategy profitability | frozen Bybit price-only evidence | `FAIL / NOT PROVEN` | 102 trades, 36 WIN / 11 BE / 55 LOSS, about `-176.67 USDT` on 1,000 USDT |
| GitHub governance | current server state | `VERIFIED_DISABLED` | `main` protection / required-status enforcement disabled; #103 |
| Live/mainnet | canonical fail-closed flags | `FAIL_CLOSED` | External routing, live trading and mainnet entry disabled |

`043fc300...` is the exact current C2A4 product/security evidence subject. Documentation-only commits after it do not by themselves promote trading capability and must not be confused with broker, strategy or operational qualification.

## Current capability matrix

| Capability | Current state | Evidence / blocker |
|---|---|---|
| Architecture cohesion | STRONG CORE; CONSOLIDATION IN PROGRESS | C2A0–C2A4 ancestry-free foundations are canonical; v121 control, v122 session-risk and supervisor/recovery layers remain bounded extraction work |
| Software correctness | STRONG PARTIAL / CURRENT BASELINE PASS | C2A4 PostgreSQL-aware full regression: `1124 passed / 2 dedicated fleet skips`; all 11 post-merge release gates green |
| Data integrity | PARTIAL | Strong deterministic validation/research contracts; authoritative derivatives PIT acquisition incomplete |
| Strategy profitability | FAIL / NOT PROVEN | Frozen Bybit replay negative; no positive untouched cost-adjusted OOS edge |
| Risk management | STRONG DETERMINISTIC | Mature deterministic pre-trade/portfolio controls; real connected and production governance evidence still incomplete |
| OMS / execution safety | STRONG DETERMINISTIC | Durable mutations, at-most-once safeguards, GET-first ambiguity recovery and fault campaigns exist historically/canonically where extracted |
| Database runtime security | C2A1–C2A4 QUALIFIED | v119 non-owner role, v120 append-only TRUNCATE hardening, typed audit persistence and v119 excursion CAS are proven on PostgreSQL 16 |
| Active-excursion persistence | QUALIFIED | C2A4 proves deterministic state/revision integrity, stale/cross-entry/tamper rejection and non-owner CAS operations |
| v121 ARM/HALT control state | C2B0 AUDIT IN PROGRESS | Historical pure persistence/evaluator semantics are useful, but old module also imports connected-preflight DTOs and an order client |
| Real external integration | BLOCKED | No current credential-backed broker proof sufficient for release |
| Demo execution | NOT PROVEN | Protected one-shot design exists historically; no canonical real Demo ENTRY evidence |
| Exact-head operational evidence | NOT PROVEN | No real canonical INFRA→SESSION→SUPERVISOR→ARM→ENTRY→HALT→RECOVERY chain |
| Reliability / soak | NOT PROVEN CONNECTED | Deterministic fault evidence exists; multi-week connected Demo soak absent |
| Supply-chain / release provenance | PASS FOR C2A4 MERGE SHA | Strict lock audit, build/release evidence, signed SLSA and SBOM on `043fc300...` |
| GitHub governance | BLOCKED | `main` protection verified disabled |
| Production infrastructure | PARTIAL / EXTERNAL EVIDENCE MISSING | KMS/HSM, backup/restore, external audit and target-environment proof incomplete |
| Live trading readiness | BLOCKED | Strategy, Demo, exact-head evidence, soak, governance and production external proof incomplete |
| Live profitability | NOT STARTED / UNPROVEN | No authorized mainnet pilot |

The matrix is diagnostic only; no row can grant a release gate by itself.

## C2A4 canonical-main evidence

Exact C2A4 pre-merge qualified head:

`00a0deffa954c70e44fb70483556746363228535`.

Exact C2A4 merge / post-merge main SHA:

`043fc3003b055dc1e953854798627048d3f26960`.

Pre-merge: **10/10** applicable PR workflows succeeded on the unchanged exact head.

Post-merge: **11/11** push workflows succeeded on the exact merge SHA.

Key post-merge evidence:

- `canonical-security-regression` run `34039454929` — PASS on PostgreSQL 16.15.
  - security migration lineage through v119/v120 applied twice successfully;
  - C2A4 Ruff and Bandit qualification passed;
  - focused security/persistence suite: **282 passed**;
  - full PostgreSQL-aware suite: **1124 passed / 2 skipped**;
  - both skips are dedicated fleet-deployment tests requiring `ASTRA_TEST_FLEET_DEPLOYMENT_DSN`;
  - runtime-role attempts to `TRUNCATE`, `ALTER TABLE` and `DROP TABLE` `astra_bybit_demo_active_excursion_v119` were rejected;
  - durable database contracts, release-lineage and fail-closed production-state checks passed.
- `release-provenance` run `34039454985` — PASS.
  - exact hash-locked dependency graph and dependency audit passed;
  - full regression and package build passed;
  - signed SLSA build provenance passed;
  - signed SBOM attestation passed.

This is deterministic engineering and database-security evidence, not connected broker evidence.

## Completed C2A foundation

### C2A0 — durable strategy-free runtime lease

PR #113 canonicalized the byte-preserved v119 singleton runtime lease. It proves single-writer ownership, exact-owner release, no TTL/stale takeover and no strategy/network/order capability.

Frozen migration SHA-256:

`c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e`.

### C2A1 — non-owner PostgreSQL runtime role

PR #116 separated bootstrap/migration authority from the long-running runtime credential. The runtime role is non-owner, lacks database/schema create and DDL/TRUNCATE authority, and has only the DML required by v119 lease/excursion tables.

### C2A2 — v120 append-only audit hardening

PR #118 preserved historical v120/001 byte-for-byte and added forward statement-level TRUNCATE rejection. Runtime v120 audit-table privileges are exactly `SELECT, INSERT`; UPDATE/DELETE/TRUNCATE/DDL remain unavailable.

Frozen v120/001 evidence:

- Git blob `b337ef19dc7da4a3fcbc0a11a8d6d7d85dff3b00`;
- SHA-256 `613a21fba9142f34141cb7d6c81938b175250fb4dbda9bd37726e5627df094cf`.

### C2A3 — strategy-free v120 typed persistence

Issue #119 / PR #121 canonicalized immutable persistence-safe approved-entry authorization, outcome-free entry provenance and reconciled terminal-evidence records plus SELECT/INSERT PostgreSQL adapters. Historical strategy selector builders, crypto strategy types, broker/market-data clients and order/ARM/mainnet capability remain excluded.

### C2A4 — strategy-free v119 active-excursion CAS persistence

Issue #122 / PR #124 is completed and post-merge qualified.

Canonicalized:

- neutral immutable `LONG|SHORT` active-excursion persistence state;
- deterministic canonical JSON and historical-compatible SHA-256 revision;
- singleton `ACTIVE` checkpoint identity;
- initialize/load/CAS-save/CAS-clear behavior;
- stale-revision, cross-entry, unknown-field, non-finite numeric and tamper rejection;
- existing C2A1 non-owner runtime role with exact SELECT/INSERT/UPDATE/DELETE privilege surface;
- explicit absence of runtime migration, broker/network, market-data, strategy, ARM/HALT, order and mainnet capability.

C2A4 therefore proves a durable persistence primitive, **not** a market-observing excursion tracker and not a trading path.

## Current gate: C2B0 — v121 ARM/HALT journal and fail-closed control state

Issue #128 is the next bounded executable unit.

Historical source PR #80 head:

`f40d9dee0baadd254a6fe7425b117be709088f9d`.

Integrity hardening source PR #88 head:

`d40e3e9b6740896cd5317c01eb9f482cb238b53f`.

Verified preservation fact: `migrations/v121/001_bybit_demo_control_plane.sql` has the same Git blob in PR #80 and PR #88:

`cae1dd432050f235b94d230b2e46c862d38b58c6`.

PR #88 did not rewrite that migration; it added a forward statement-level no-TRUNCATE guard. C2B0 must preserve the frozen v121 bytes and reproduce the hardening as a separate auditable forward layer.

The historical Python module cannot be copied wholesale because it imports:

- `BybitDemoOrderRequest`;
- connected-preflight result/status DTOs;
- `ControlPlaneGuardedBybitDemoClient`, whose `place_market_order()` delegates to broker mutation after an ARM check.

C2B0 may extract only:

1. immutable neutral ARM/HALT event records;
2. deterministic event and preflight-payload SHA-256 validation;
3. append/read PostgreSQL journal behavior;
4. a read-only fail-closed decision evaluator;
5. short-lived ARM timing constraints and expired/malformed/default-HALT semantics;
6. row-level UPDATE/DELETE immutability plus forward physical no-TRUNCATE hardening;
7. least-privilege non-owner database access.

Explicitly excluded from C2B0: connected preflight acquisition, fixed-egress readiness, order-client guard, actual order mutation, session-start, v122 session risk, v123 lease recovery, automatic ARM, strategy/risk-economics changes and mainnet.

## Work after C2B0

### C2B1+ — remaining control/session-risk durability

Historical sources #84/#85/#86 retain separate semantics that must not be folded blindly into C2B0:

- v122 immutable opening equity and monotonic high-water session-risk ledger;
- one-time flat/HALTED connected session initialization;
- terminal evidence → risk commit → exact checkpoint ACK ordering.

These require their own dependency/security boundaries, especially because PR #85 uses fixed-egress read-only Bybit evidence.

### C2C — active-trade supervision and controlled recovery

Historical sources #87/#88. Recovery exists to return the system to a known safe state; it cannot create a second ENTRY, auto-ARM or silently erase active excursion/session-risk truth.

### C1 — canonical GET-only broker identity/readiness

Historical sources include #78/#81/#82 and later final readiness forms. Target is environment/account identity, server-time/clock health, read-only account/activity evidence and protected fixed egress, with no order create/amend/cancel surface.

### C3 — canonical protected Demo entry composition

Source semantics: PR #89. Do not start until canonical C1/C2 foundations **and** strategy-promotion prerequisites pass. It must preserve at-most-one ENTRY attempt, no blind mutation retry, exact authorization/provenance, reconciliation-first ambiguity handling, reduce-only/protection recovery and no second ENTRY from recovery.

### C4 — exact-head operational evidence

Source semantics: PR #90. Required real evidence order remains:

```text
INFRA_READY
→ SESSION_READY
→ SUPERVISOR_READY
→ ARM_PROVEN
→ DEMO_ENTRY_PROVEN
→ HALT_PROVEN
→ RECOVERY_DRILL_PROVEN
```

Every stage must bind to one exact canonical SHA and artifact hashes.

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

This does not support strategy promotion. Incomplete derivatives-context research cannot supersede it without authoritative point-in-time acquisition and a new frozen validation protocol.

## Current formal blockers

### P0

- branch protection / required review enforcement — #103;
- positive strategy edge — FAIL / not proven;
- real protected Demo entry — not proven;
- exact-head real-broker evidence — not proven;
- independent live approval / tiny-cap pilot — absent.

### P1

- C2B0 v121 control-state isolation — #128;
- V107–V109 append-only TRUNCATE hardening — #109;
- authoritative derivatives PIT evidence;
- connected Demo soak;
- production KMS/HSM, backup/restore, external audit, observability and incident evidence.

## Historical evidence archive

The previously current, much larger E2E matrix remains preserved byte-for-byte at:

`docs/archive/2026-09-02/E2E_MATRIX_PRE_C2A0.md`

That snapshot remains valid historical evidence for the exact heads/runs it names, but it cannot override this current matrix.
