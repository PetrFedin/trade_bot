# Stacked PR Consolidation Map

This document is the **current** human-readable extraction plan for issue #104. The full pre-C2A0 range inventory is preserved byte-for-byte at `docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_MAP_V1.md`; the matching machine snapshot is `docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_STATUS_V1.json`.

## Current state — 2026-09-06

- canonical qualified `main`: `043fc3003b055dc1e953854798627048d3f26960`;
- C2A0: **EXTRACTED_AND_QUALIFIED** through PR #113;
- C2A1: **EXTRACTED_AND_QUALIFIED** through PR #116;
- C2A2: **EXTRACTED_AND_QUALIFIED** through PR #118;
- C2A3: **EXTRACTED_AND_QUALIFIED** through PR #121;
- C2A4: **EXTRACTED_AND_QUALIFIED** through PR #124, with 11/11 post-merge workflows successful;
- current executable gate: **C2B0 / issue #128** — strategy-free v121 ARM/HALT journal and fail-closed control state;
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
8. No C1/C2 infrastructure primitive can authorize an ENTRY by itself.
9. Every replacement requires exact-head pre-merge qualification and post-merge `main` qualification.
10. C3 must preserve PR #89 at-most-one/no-blind-resubmit semantics.
11. C4 must preserve PR #90 exact-head evidence semantics.
12. A completed deterministic extraction is not connected broker evidence.
13. Profitability remains a separate gate.
14. Frozen historical migrations are preserved byte-for-byte; later security hardening is forward-only.
15. Connected-preflight acquisition and broker/order authority must remain separate from persistence-safe control-state extraction.

## Completed C2A foundation

### C2A0 — durable strategy-free runtime lease

PR #113 extracted the audited v119 runtime-lease primitive onto canonical main without inheriting stacked research ancestry.

Frozen migration SHA-256:

`c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e`.

It proves singleton durable ownership, exact-owner release, no TTL/age takeover and no strategy/network/order capability.

### C2A1 — v119 runtime-role least privilege

PR #116 separated bootstrap/migration authority from the long-running PostgreSQL runtime credential.

The runtime role is non-owner, lacks database/schema create, DDL and TRUNCATE authority, and receives only the DML required by the canonical v119 lease/excursion schema.

### C2A2 — v120 append-only audit hardening

PR #118 preserved historical v120/001 byte-for-byte and added a forward `v120/002` statement-level TRUNCATE guard.

Frozen v120/001 evidence:

- Git blob: `b337ef19dc7da4a3fcbc0a11a8d6d7d85dff3b00`;
- SHA-256: `613a21fba9142f34141cb7d6c81938b175250fb4dbda9bd37726e5627df094cf`.

The runtime role has exactly `SELECT, INSERT` on the three v120 audit tables and no update/delete/truncate/DDL authority.

### C2A3 — strategy-free v120 typed persistence

Issue #119 / PR #121 canonicalized only persistence-safe immutable authorization, outcome-free provenance and terminal-evidence records plus PostgreSQL SELECT/INSERT adapters.

Qualified merge SHA:

`090f34b11a877ce24f8a15a74b296e287aae3918`.

Historical strategy selector builders, crypto strategy types, broker/market-data clients and order/ARM/mainnet capability remain excluded.

### C2A4 — strategy-free v119 active-excursion checkpoint/CAS persistence

Issue #122 / PR #124 is completed.

Final pre-merge head:

`00a0deffa954c70e44fb70483556746363228535`.

Qualified merge SHA:

`043fc3003b055dc1e953854798627048d3f26960`.

C2A4 canonicalized only:

- neutral immutable active-excursion persistence state;
- deterministic canonical JSON and historical-compatible SHA-256 revision;
- singleton `ACTIVE` checkpoint identity;
- initialize/load/CAS-save/CAS-clear PostgreSQL behavior;
- stale-revision, cross-entry and tamper rejection;
- existing C2A1 non-owner runtime-role boundary.

It deliberately excluded historical `CryptoSide`, `CryptoTradePlan`, broker position/monitor types, Bybit market-data quote types and observation/finalization builders.

Post-merge evidence on the exact merge SHA:

- 11/11 push workflows SUCCESS;
- `canonical-security-regression` run `34039454929` — PostgreSQL 16.15, **282 focused passed**, **1124 full passed / 2 dedicated fleet skips**;
- runtime-role TRUNCATE/ALTER/DROP attempts against the v119 active-excursion table rejected;
- `release-provenance` run `34039454985` — full qualification, signed SLSA and signed SBOM PASS.

C2A4 is a persistence primitive, not market observation or trading authority.

## Current: C2B0 — v121 ARM/HALT journal and fail-closed control state

Issue #128 is the next bounded implementation unit.

Primary historical source: PR #80 head:

`f40d9dee0baadd254a6fe7425b117be709088f9d`.

Final v121 integrity hardening source: PR #88 head:

`d40e3e9b6740896cd5317c01eb9f482cb238b53f`.

### Preservation result already established

`migrations/v121/001_bybit_demo_control_plane.sql` is the same Git blob in PR #80 and PR #88:

`cae1dd432050f235b94d230b2e46c862d38b58c6`.

The original v121 migration creates an append-only ARM/HALT event journal and blocks row-level UPDATE/DELETE, but it has no statement-level TRUNCATE guard. PR #88 did **not** rewrite v121/001. Its v123 migration added `astra_bybit_demo_control_no_truncate_v123` as a forward `BEFORE TRUNCATE` guard.

C2B0 must therefore preserve v121/001 byte-for-byte and add a separate forward hardening artifact; the exact historical SHA-256 must be independently measured and frozen before implementation.

### Historical mixed dependency problem

The final historical `app/execution/bybit_demo_control_plane.py` is not safe to copy wholesale. It imports:

- `BybitDemoOrderRequest`;
- connected-preflight result/status DTOs.

It also contains `ControlPlaneGuardedBybitDemoClient.place_market_order()`, which performs the repeated ARM check and then delegates to broker order mutation.

That bridge is operationally important later, but it is outside the persistence-safe C2B0 boundary.

### C2B0 extraction boundary

C2B0 may canonicalize only:

1. neutral immutable ARM/HALT persistence records;
2. deterministic canonical JSON/event SHA-256 identity;
3. independent hash validation of already-sanitized preflight evidence;
4. PostgreSQL append/read behavior;
5. missing/malformed/forged/expired state => HALTED;
6. valid short-lived ARM => ARMED_NEW_ENTRIES as a read-only control decision only;
7. historical ARM constraints such as <=300 second TTL and <=30 second preflight age where preserved by audit;
8. row-level UPDATE/DELETE immutability;
9. separate forward physical no-TRUNCATE hardening;
10. explicit least-privilege non-owner database roles.

C2B0 must not import or expose:

- connected Bybit preflight acquisition;
- broker REST/WebSocket or order DTO/client;
- `place_market_order` or any exchange mutation;
- fixed-egress trading-credential readiness;
- v122 session risk;
- PR #85 session start;
- PR #86 terminal runtime handoff;
- v123 lease recovery itself;
- automatic ARM;
- strategy/ranking/sizing/risk-economics changes;
- mainnet.

## Remaining work packages after C2B0

### C2B1 — v122 restart-safe session-risk persistence

Primary historical source: PR #84.

Candidate bounded semantics:

- immutable opening equity;
- monotonic equity high-water;
- append-only terminal outcome journal;
- CAS revision and restart reconstruction;
- no automatic initialize/reset/takeover;
- no broker/order capability.

This should be separated from PR #85 connected session-start semantics.

### C2B2 — one-time session-start boundary

Primary historical source: PR #85.

This layer is connected-readiness work: flat/HALTED initialization uses fixed-egress Demo **read-only** evidence and therefore cannot be silently folded into database-only C2B0/C2B1.

### C2B3 — terminal evidence → risk commit → checkpoint ACK

Primary historical source: PR #86.

The crash-safe ordering is required, but its runtime dependency closure must be audited against the canonical C2A3/C2A4 types before implementation.

### C2C — active-trade supervision and controlled recovery

Historical sources: #87/#88.

Must preserve the rule that recovery exists only to return the system to a known safe state. It cannot create a second ENTRY, auto-ARM, reset session risk or clear the active excursion merely to unblock trading.

### C1 — canonical GET-only broker identity/readiness

Historical sources include #78/#81/#82 and final readiness composition from later prerequisite work.

Target:

- environment/account identity;
- broker server-time/clock health;
- read-only account/activity evidence;
- protected fixed egress and credential/IP binding;
- no order create/amend/cancel surface;
- no credential escalation.

C1 remains independent from C3 order authority.

### C3 — canonical protected Demo entry composition

Source semantics: PR #89.

Do not start until canonical C1/C2 foundations **and** strategy-promotion prerequisites are satisfied.

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

Even a deterministic C3 PASS remains `DEMO_UNPROVEN` until an explicitly authorized protected connected execution succeeds.

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
- #128 — C2B0 v121 control-state isolation is the current **P1** executable consolidation gate.
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

No historical stack PR should be mass-closed merely because C2A0–C2A4 now exist.

## What is explicitly not authorized

- bulk merge of historical PR #93 ancestry;
- strategy promotion from research PRs;
- Demo order submission;
- mainnet order capability;
- automatic ARM;
- leverage/capital increase;
- weakening risk controls;
- calling C2A0–C2A4 a production trading runtime;
- claiming profitability.
