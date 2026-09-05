# Stacked PR Consolidation Map

This document is the **current** human-readable extraction plan for issue #104. The full pre-C2A0 range inventory is preserved byte-for-byte at `docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_MAP_V1.md`; the matching machine snapshot is `docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_STATUS_V1.json`.

## Current state — 2026-09-05

- canonical qualified `main`: `090f34b11a877ce24f8a15a74b296e287aae3918`;
- C2A0: **EXTRACTED_AND_QUALIFIED** through PR #113;
- C2A1: **EXTRACTED_AND_QUALIFIED** through PR #116;
- C2A2: **EXTRACTED_AND_QUALIFIED** through PR #118;
- C2A3: **EXTRACTED_AND_QUALIFIED** through PR #121, with 11/11 post-merge workflows successful;
- current executable gate: **C2A4 / issue #122** — strategy-free v119 active-excursion checkpoint/CAS persistence;
- old PR #110: **CLOSED_NOT_MERGED**, superseded only after exact blob preservation and independent qualification;
- historical operational decomposition source: PR #93 / `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b`;
- research head: PR #100 / `918dbc57c0633c6dc549f1f036d2ae659b289b46`, research-only;
- canonical V107–V109 append-only TRUNCATE hardening remains tracked separately by #109;
- `main` branch protection remains verified disabled under #103;
- no real Demo ENTRY, mainnet write or strategy promotion has occurred.

## Consolidation invariants

1. Never merge the historical #41–#100 stack as an ancestry bundle.
2. Every operational extraction starts from current canonical `main`.
3. Preserve unique code/migrations/tests/evidence before closing a source PR.
4. Reuse exact Git blobs where semantics are already correct; do not recreate equivalent version layers without need.
5. A historical qualified head is evidence for its own revision only.
6. Research ancestry is not accepted merely because operational code is stacked on top of it.
7. No extraction may change strategy/risk economics unless that change is separately scoped and validated.
8. No C1/C2 infrastructure primitive can authorize an ENTRY.
9. Every replacement requires exact-head pre-merge qualification and post-merge `main` qualification.
10. C3 must preserve PR #89 at-most-one/no-blind-resubmit semantics.
11. C4 must preserve PR #90 exact-head evidence semantics.
12. A completed deterministic extraction is not connected broker evidence.
13. Profitability remains a separate gate.

## Completed C2A foundation

### C2A0 — durable strategy-free runtime lease

PR #113 extracted the audited v119 runtime-lease primitive onto canonical main without inheriting stacked research ancestry.

Frozen migration SHA-256:

`c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e`.

It proves singleton durable ownership, exact-owner release, no TTL/age takeover and no strategy/network/order capability.

### C2A1 — v119 runtime-role least privilege

PR #116 separated bootstrap/migration authority from the long-running PostgreSQL runtime credential.

The runtime role is non-owner, lacks database/schema create, DDL and TRUNCATE authority, and receives only the DML required by the canonical v119 lease/excursion schema. This is a database-security primitive, not broker readiness.

### C2A2 — v120 append-only audit hardening

PR #118 preserved historical v120/001 byte-for-byte and added a forward `v120/002` statement-level TRUNCATE guard.

Frozen v120/001 evidence:

- Git blob: `b337ef19dc7da4a3fcbc0a11a8d6d7d85dff3b00`;
- SHA-256: `613a21fba9142f34141cb7d6c81938b175250fb4dbda9bd37726e5627df094cf`.

The runtime role has exactly `SELECT, INSERT` on the three v120 audit tables and no update/delete/truncate/DDL authority.

### C2A3 — strategy-free v120 typed persistence

Issue #119 / PR #121 is completed.

Final pre-merge head:
`f623bea57f0d9e27136658262d85c0579db270d3`.

Qualified merge SHA:
`090f34b11a877ce24f8a15a74b296e287aae3918`.

C2A3 canonicalized only persistence-safe typed contracts and PostgreSQL adapters:

- approved-entry authorization;
- outcome-free entry provenance;
- fully reconciled terminal diagnostics;
- canonical JSON / SHA-256 identity;
- exact-key and tamper validation;
- idempotent INSERT / SELECT rehydration;
- existing C2A2 least-privilege runtime role.

It deliberately excluded historical strategy selector builders, crypto strategy types, broker/market-data clients, cycle decision logic and any order/ARM/mainnet capability.

Post-merge evidence on the exact merge SHA:

- 11/11 push workflows SUCCESS;
- `canonical-security-regression` run `33988217105` — PostgreSQL 16, **271 focused passed**, **1112 full passed / 2 dedicated fleet skips**;
- `release-provenance` run `33988217099` — locked dependency graph, full regression, build, signed SLSA and signed SBOM PASS.

## Current: C2A4 — v119 active-excursion checkpoint/CAS persistence

Issue #122 is the next bounded implementation unit.

Historical source: PR #76 head `84b9965e5128562c49620c6164afe5f43ca343f2`.

The useful historical persistence primitive is a singleton `ACTIVE` checkpoint in `astra_bybit_demo_active_excursion_v119` with:

- deterministic revision binding entry `orderLinkId` to canonical state payload;
- initialize via insert-if-absent;
- load with checksum and safety-marker validation;
- compare-and-swap UPDATE using exact expected revision;
- compare-and-swap DELETE using exact expected revision;
- stale/concurrent revision rejection;
- diagnostics-only / no-retuning / no-mainnet safety markers.

The historical supporting files are **not** safe to copy wholesale because they mix persistence with:

- `CryptoSide`;
- `CryptoTradePlan`;
- broker position/monitor types;
- Bybit market-data quote types;
- observation/finalization calculations.

C2A4 therefore extracts only:

1. neutral immutable active-excursion state values;
2. deterministic canonical JSON and revision identity;
3. singleton initialize/load/CAS-save/CAS-clear PostgreSQL adapter;
4. stale-revision, cross-entry and tamper rejection;
5. the already-qualified C2A1 non-owner runtime role.

C2A4 must expose **no** `migrate()` or runtime DDL, strategy import, broker/network import, market-data dependency, order write, ARM/HALT, approval or mainnet capability.

## Remaining work packages after C2A4

### C2B — control/session-risk durability

Historical sources: #80/#84/#85/#86.

Expected bounded semantics include v121 ARM/HALT journaling, repeated final ARM checks, v122 session-risk ledger, flat/HALTED session initialization and terminal-evidence → risk-commit → checkpoint-ack ordering. These are not yet canonical.

### C2C — active-trade supervision and controlled recovery

Historical sources: #87/#88.

Must preserve the rule that recovery exists to return the system to a known safe state; it cannot create a second ENTRY.

### C1 — canonical GET-only broker identity/readiness

Historical sources include #78/#81/#82 and final readiness composition from later prerequisite work.

Target:

- environment/account identity;
- broker server-time/clock health;
- read-only account/activity evidence;
- no order create/amend/cancel surface;
- no credential escalation.

C1 remains independent from C3 order authority.

### C3 — canonical protected Demo entry composition

Source semantics: PR #89.

Do not start until the canonical C1/C2 foundations **and** strategy-promotion prerequisites are satisfied.

Must preserve:

- protected fixed egress;
- exact existing ARM state;
- short-lived explicit approval;
- immutable pre-submit authorization/provenance;
- `SUBMIT_STARTED` before broker POST;
- at most one risk-adding ENTRY attempt;
- no blind retry after ambiguous mutation;
- broker query/reconciliation before recovery decision;
- protection restoration / policy-authorized reduce-only risk reduction only;
- recovery cannot create a second ENTRY;
- no auto-ARM;
- no strategy/risk-rule changes in the operational bridge;
- no mainnet write path.

Even a deterministic C3 PASS would remain `DEMO_UNPROVEN` until an explicitly authorized protected connected execution succeeds.

### C4 — exact-head operational evidence

Source semantics: PR #90.

Required real evidence order:

```text
INFRA_READY
→ SESSION_READY
→ SUPERVISOR_READY
→ ARM_PROVEN
→ DEMO_ENTRY_PROVEN
→ HALT_PROVEN
→ RECOVERY_DRILL_PROVEN
```

Every stage must bind to one exact canonical SHA and artifact hashes. Missing, contradictory or mixed-head evidence fails closed.

### C5 — research lineage isolation

Research remains independent from operational release authority. PR #100 is research-only and the current frozen price-only result is negative. Derivatives context may become a new candidate only after authoritative point-in-time acquisition and a separately frozen validation protocol.

## Independent blockers

- #103 — `main` branch protection / required review enforcement remains disabled: **P0**.
- #109 — physical TRUNCATE hardening for canonical V107–V109 event tables remains **P1**.
- strategy edge remains **FAIL / NOT PROVEN**.
- connected Demo execution, exact-head broker evidence, soak and production external evidence remain unproven.

## Closure policy

A source PR may be closed as superseded only after:

1. exact source head and changed files are audited;
2. unique executable/migration/evidence content is preserved or explicitly rejected with rationale;
3. the replacement is based on current canonical main;
4. replacement exact-head CI passes;
5. replacement is merged with the expected qualified head;
6. post-merge main qualification passes;
7. source PR receives traceability to the replacement.

No historical stack PR should be mass-closed merely because C2A0–C2A3 now exist.

## What is explicitly not authorized

- bulk merge of historical PR #93 ancestry;
- strategy promotion from research PRs;
- Demo order submission;
- mainnet order capability;
- automatic ARM;
- leverage/capital increase;
- weakening risk controls;
- calling C2A0–C2A3 a production trading runtime;
- claiming profitability.
