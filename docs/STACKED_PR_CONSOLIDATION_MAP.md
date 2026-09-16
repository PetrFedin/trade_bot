# Stacked PR Consolidation Map

This document is the **current** human-readable extraction plan for issue #104. Historical inventory remains preserved byte-for-byte under `docs/archive/2026-09-02/`.

## Current state — 2026-09-06

- canonical qualified `main`: `ff684ab85b92151c215c7d5cc85bfc527fecb5eb`;
- C2A0: **EXTRACTED_AND_QUALIFIED** through PR #113;
- C2A1: **EXTRACTED_AND_QUALIFIED** through PR #116;
- C2A2: **EXTRACTED_AND_QUALIFIED** through PR #118;
- C2A3: **EXTRACTED_AND_QUALIFIED** through PR #121;
- C2A4: **EXTRACTED_AND_QUALIFIED** through PR #124;
- C2B0: **EXTRACTED_AND_QUALIFIED** through PR #127 / issue #125;
- current executable gate: **C2B1 / issue #130** — strategy-free v122 restart-safe session-risk persistence;
- historical operational source: PR #93 / `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b`;
- research head: PR #100 / `918dbc57c0633c6dc549f1f036d2ae659b289b46`, research-only;
- #103 branch protection remains P0;
- #109 v107-v109 physical TRUNCATE hardening remains P1;
- no real Demo ENTRY, mainnet write or strategy promotion has occurred.

## Consolidation invariants

1. Never merge the historical #41–#100 stack wholesale.
2. Every extraction starts from current qualified canonical `main`.
3. Preserve exact historical bytes before changing semantics.
4. Repair historical security gaps only forward; never rewrite frozen migrations.
5. Research ancestry cannot enter operational extraction by convenience.
6. Persistence gates cannot silently import broker/order or strategy authority.
7. Runtime credentials cannot inherit bootstrap/migration/DDL authority.
8. Append-only means UPDATE/DELETE **and TRUNCATE** resistance.
9. Every replacement requires exact-head pre-merge and actual-main post-merge qualification.
10. Profitability, connected broker evidence and live authority remain separate gates.

## Completed C2A foundation

C2A0–C2A4 remain canonical and qualified at their recorded exact merge SHAs. They provide v119 lease/checkpoint durability, v119 least privilege, v120 append-only hardening and strategy-free v120 typed persistence. They do not prove broker execution or profitability.

## Completed C2B0 — v121 append-only ARM/HALT journal

Issue #125 / PR #127 is completed.

Final pre-merge head:

`e9df55204d5127e585166fd364612ab940d114a1`

Qualified merge SHA:

`ff684ab85b92151c215c7d5cc85bfc527fecb5eb`

Preserved evidence:

- v121/001 Git blob `cae1dd432050f235b94d230b2e46c862d38b58c6`;
- v121/001 SHA-256 `a03a738d7036c59338ef2ebe085a28a266fd0b440d97ca8e3fae59379efe8e21`;
- final canonical-security workflow SHA-256 `5977182e1ae6fab8da08b5b1addbf7bb23a02af0477332e32f92349cd2ea61dd`.

C2B0 canonicalized only:

- byte-preserved v121/001;
- forward v121/002 `BEFORE TRUNCATE` hardening;
- neutral immutable ARM/HALT records and deterministic event IDs;
- strict canonical neutral preflight evidence preserving historical READY/read-only/flat/idle/schema/no-order/no-mainnet invariants;
- read-only journal reader and append-only writer;
- reader = SELECT only;
- writer = SELECT + INSERT and sequence USAGE only;
- exact `public` trigger-function and `BEFORE` timing readiness;
- wrong-schema trigger and stored hash/event-id tamper rejection.

Qualification:

- 10/10 exact-head PR workflows SUCCESS;
- 11/11 post-merge push workflows SUCCESS;
- canonical-security `34056249717` SUCCESS;
- release-provenance `34056249725` qualify + signed SLSA/SBOM SUCCESS;
- canonical-deployment `34056249729` SUCCESS.

C2B0 contains no connected preflight acquisition, broker/order client, strategy/risk change, session-risk, session-start, recovery or mainnet capability.

## Current: C2B1 — strategy-free v122 restart-safe session-risk persistence

Issue #130 is the next bounded implementation unit.

Primary source: PR #84 head `bdcdf7189b56b494e14ac746ad9b867c108dcd30`.

Persistence refinement: PR #86 head `700e59e0b67329ea7df00bbd78aebd6ddfdba334`; only persistence-safe `load_active()` is eligible from this later patch. PR #86 runtime terminal handoff is not C2B1.

Verified frozen v122/001 Git blob at PR #84 and PR #88:

`25f05f93cf11165416e999321ac5d4a422b9a1a3`

Runner-side SHA-256 must be measured before implementation.

### Historical database semantics worth preserving

The frozen v122 migration already has physical integrity controls:

- active singleton ledger `astra_bybit_demo_session_risk_v122`;
- immutable positive opening equity;
- peak equity cannot decrease;
- outcome count cannot decrease;
- canonical checkpoint + SHA-256 revision;
- active DELETE and TRUNCATE rejected;
- outcome journal is append-only with UPDATE/DELETE/TRUNCATE rejection.

### Mixed dependency problem

Historical `bybit_demo_session_risk_ledger.py` imports:

- `CryptoSessionRiskState`;
- post-trade accounting result/outcome types.

It also contains runtime/business builders:

- strategy risk-state conversion;
- exchange wallet/equity observation;
- post-trade accounting → session ledger mutation.

These stay outside C2B1.

### C2B1 extraction boundary

C2B1 may canonicalize only:

1. neutral immutable terminal-outcome persistence records;
2. neutral immutable session ledger/checkpoint records;
3. deterministic compact sorted JSON and SHA-256;
4. finite Decimal, identity and ordering validation;
5. one-time zero-outcome initialization;
6. `load_active()` without invented opening equity;
7. CAS save with stale-revision rejection;
8. checkpoint/outcome-journal cross-verification on restart;
9. immutable opening equity and non-decreasing peak equity;
10. exact non-owner runtime privileges.

Explicitly excluded:

- `CryptoSessionRiskState`;
- post-trade accounting-derived builders;
- wallet/equity acquisition;
- PR #85 connected session start;
- PR #86 terminal runtime handoff;
- broker/order/recovery/mainnet capability.

## Follow-on decomposition

### C2B2 — connected one-time session start

Source: PR #85. Uses fixed-egress authenticated Demo read-only account evidence and therefore belongs after/with canonical C1 GET-only broker observability, not inside persistence-only C2B1.

### C2B3 — terminal evidence → risk commit → checkpoint ACK

Source: PR #86. Preserve crash-safe ordering and idempotent retry only after C2B1 persistence is canonical.

### C2C — active-trade supervision and controlled recovery

Sources #87/#88. Recovery may only restore known-safe state and can never create a second ENTRY.

### C1 — canonical GET-only broker observability

Historical sources include #78/#81/#82. It remains independent from order authority.

### C3 — protected Demo entry composition

Source semantics PR #89. Blocked until canonical C1/C2 foundations and strategy-promotion prerequisites pass.

### C4 — exact-head operational evidence

Source semantics PR #90. Real evidence order remains:

```text
INFRA_READY
→ SESSION_READY
→ SUPERVISOR_READY
→ ARM_PROVEN
→ DEMO_ENTRY_PROVEN
→ HALT_PROVEN
→ RECOVERY_DRILL_PROVEN
```

### C5 — research isolation

PR #100 remains research-only. Current frozen strategy evidence remains negative.

## Independent blockers

- #103 — `main` branch protection disabled: **P0**;
- #109 — v107-v109 physical TRUNCATE hardening: **P1**;
- #130 — C2B1 v122 persistence: current **P1** executable gate;
- strategy edge: **FAIL / NOT PROVEN**;
- connected Demo execution, exact-head broker evidence, soak and production proof remain unproven.

## Explicitly not authorized

- bulk historical merge/cherry-pick;
- strategy promotion;
- Demo order submission;
- mainnet order capability;
- automatic ARM;
- leverage/capital increase;
- weakening risk controls;
- claiming profitability.
