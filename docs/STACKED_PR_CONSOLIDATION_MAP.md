# Stacked PR Consolidation Map

This document is the **current** human-readable extraction plan for issue #104. The full pre-C2A0 range inventory is preserved byte-for-byte at `docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_MAP_V1.md`; the matching machine snapshot is `docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_STATUS_V1.json`.

## Current state — 2026-09-03

- canonical `main`: `e110a4c02f5bf9b9937ff3fbf7e942859be9050d`;
- C2A0: **EXTRACTED_AND_QUALIFIED** through PR #113;
- old PR #110: **CLOSED_NOT_MERGED**, superseded only after exact blob preservation and independent qualification;
- historical operational decomposition source: PR #93 / `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b`;
- research head: PR #100 / `918dbc57c0633c6dc549f1f036d2ae659b289b46`, research-only;
- next executable gate: C2A1 / issue #114;
- v120 extraction remains blocked by #107;
- canonical V107–V109 append-only TRUNCATE hardening remains tracked by #109;
- no Demo, mainnet or strategy promotion has occurred.

## Consolidation invariants

1. Never merge the historical #41–#100 stack as an ancestry bundle.
2. Every operational extraction starts from current canonical `main`.
3. Preserve unique code/migrations/tests/evidence before closing a source PR.
4. Reuse exact Git blobs where semantics are already correct; do not recreate equivalent version layers.
5. A historical qualified head is evidence for its own revision only.
6. Research ancestry is not accepted merely because operational code is stacked on top of it.
7. No extraction may change strategy/risk economics unless that change is separately scoped and validated.
8. No C1/C2 infrastructure primitive can authorize an ENTRY.
9. C3 must preserve PR #89 at-most-one/no-blind-resubmit semantics.
10. C4 must preserve PR #90 exact-head evidence semantics.
11. A completed deterministic extraction is not connected broker evidence.
12. Profitability remains a separate gate.

## Completed: C2A0 — durable strategy-free runtime lease

PR #113 extracted exactly five audited blobs from historical PR #110 onto repaired canonical main:

- `app/execution/bybit_demo_runtime_lease.py`;
- `app/execution/bybit_demo_postgres_runtime_lease.py`;
- `migrations/v119/001_bybit_demo_durable_runtime.sql`;
- `tests/test_bybit_demo_runtime_lease.py`;
- `tests/test_bybit_demo_postgres_runtime_lease_v119.py`.

The historical migration remains byte-identical with SHA-256:

`c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e`.

The replacement branch was based directly on repaired canonical main and inherited no research ancestry. Old PR #110 was closed only **after** #113 merged and post-merge current-main qualification passed.

C2A0 proves a single-writer durable lease primitive. It deliberately does not include the historical typed active-excursion adapter because that adapter reaches `CryptoTradePlan` strategy types.

## Next: C2A1 — v119 runtime-role least privilege

Issue #114 is the next bounded implementation unit under parent database-security issue #107.

Goal: canonical v119 must operate with a long-running runtime credential that is not the schema/table owner and cannot use ownership/DDL to bypass safety controls.

Required runtime-role contract:

- distinct from bootstrap/migration authority;
- non-superuser;
- no CREATEDB/CREATEROLE/BYPASSRLS privilege unless independently justified (none is currently justified);
- schema `USAGE` only, no schema `CREATE`;
- lease table: exact SELECT/INSERT/DELETE required by canonical lease API; no UPDATE/TRUNCATE;
- active-excursion table: only the exact DML later proven necessary by the audited checkpoint CAS API;
- no table ownership;
- no `ALTER TABLE`, `DROP TABLE` or `TRUNCATE` through runtime credentials;
- fail-closed privilege preflight before a future production composition treats the role as operationally ready.

The frozen v119/001 migration is not edited. Bootstrap authority and runtime authority must remain separate.

## v120 remains blocked

Historical PR #77 contains valuable approval/provenance/terminal-evidence stores, but its v120 append-only tables were designed with UPDATE/DELETE triggers and do not by themselves close owner/TRUNCATE bypass.

Therefore v120 canonicalization is prohibited until #107 produces and qualifies:

- append-only TRUNCATE protection;
- runtime non-owner role separation;
- deterministic privilege preflight;
- real PostgreSQL proof that runtime cannot bypass immutability.

Do not solve this by editing the frozen v120/001 migration in place. Use a forward hardening layer or an explicit bootstrap/privilege policy with preserved historical lineage.

## Remaining work packages

### C1 — canonical GET-only broker observability

Sources: historical R3/R7 (#50/#51/#53/#74).

Target:
- environment/account identity;
- broker time/clock health;
- read-only account/activity evidence;
- no order create/amend/cancel surface;
- no credential escalation.

This should be canonicalized independently of C3 order authority.

### C2 — canonical durable operational control plane

Status: **IN PROGRESS**.

- C2A0 complete: durable v119 lease;
- C2A1 next: non-owner least-privilege v119 runtime role;
- later slices: durable approvals/provenance, control-plane HALT/ARM, session-risk state, supervisor and controlled recovery — only after their DB/security prerequisites are qualified.

### C3 — canonical protected Demo entry composition

Source semantics: PR #89.

Do not start until C1/C2 foundations and the strategy-promotion prerequisites are satisfied.

Must preserve:
- protected fixed egress;
- existing exact ARM state;
- <=120s explicit approval;
- immutable pre-submit authorization/provenance;
- `SUBMIT_STARTED` before broker POST;
- at most one risk-adding ENTRY attempt;
- no blind retry after ambiguous mutation;
- broker query/reconciliation before recovery decision;
- protection restoration / policy-authorized reduce-only risk reduction only;
- recovery cannot create a second ENTRY;
- no auto-ARM;
- no ranked fallback;
- no strategy/risk-rule changes in the operational bridge;
- no mainnet write path.

A deterministic PASS here would still be `DEMO_UNPROVEN` until an explicit protected connected execution succeeds.

### C4 — exact-head operational evidence

Source semantics: PR #90.

Required real evidence order:

```text
INFRA_READY
-> SESSION_READY
-> SUPERVISOR_READY
-> ARM_PROVEN
-> DEMO_ENTRY_PROVEN
-> HALT_PROVEN
-> RECOVERY_DRILL_PROVEN
```

Every stage must bind to one exact canonical SHA and source artifact hashes. Missing, contradictory or mixed-head evidence fails closed.

### C5 — research lineage isolation

Research remains independent from operational release authority. PR #100 is research-only and the latest frozen price-only result is negative. Derivatives context may become a new hypothesis/evidence set only after authoritative point-in-time acquisition and frozen validation.

## Security work that remains independent of C2A1

Issue #109 tracks owner/TRUNCATE hardening for canonical V107–V109 append-only event tables. It remains a P1 production-security blocker even though the current deterministic security tests pass their existing UPDATE/DELETE immutability contract.

## Closure policy

A source PR may be closed as superseded only after:

1. exact current/source head and changed files were audited;
2. unique executable/migration/evidence content is preserved or explicitly rejected with rationale;
3. the replacement is based on current canonical main;
4. replacement exact-head CI passes;
5. replacement is merged;
6. post-merge main qualification passes;
7. source PR receives a traceability comment naming the replacement.

PR #110 is the first completed example of this rule. No other historical stack PR should be mass-closed because C2A0 exists.

## What is explicitly not authorized

- bulk merge of #93;
- strategy promotion from #94–#100;
- Demo order submission;
- mainnet order capability;
- automatic ARM;
- leverage/capital increase;
- weakening risk controls;
- calling C2A0 a production trading runtime;
- claiming profitability.
