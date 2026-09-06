# Stacked PR Consolidation Map

This document is the **current** human-readable extraction plan for issue #104. The full pre-C2A0 range inventory remains preserved byte-for-byte at `docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_MAP_V1.md`; the matching machine snapshot is `docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_STATUS_V1.json`.

## Current state — 2026-09-06

- canonical qualified `main`: `043fc3003b055dc1e953854798627048d3f26960`;
- C2A0: **EXTRACTED_AND_QUALIFIED** through PR #113;
- C2A1: **EXTRACTED_AND_QUALIFIED** through PR #116;
- C2A2: **EXTRACTED_AND_QUALIFIED** through PR #118;
- C2A3: **EXTRACTED_AND_QUALIFIED** through PR #121;
- C2A4: **EXTRACTED_AND_QUALIFIED** through PR #124 / issue #122;
- current executable gate: **C2B0 / issue #125** — strategy-free v121 append-only ARM/HALT control-journal persistence;
- historical operational decomposition source: PR #93 / `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b`;
- research head: PR #100 / `918dbc57c0633c6dc549f1f036d2ae659b289b46`, research-only;
- canonical V107–V109 append-only TRUNCATE hardening remains tracked separately by #109;
- `main` branch protection remains verified disabled under #103;
- no real Demo ENTRY, mainnet write or strategy promotion has occurred.

## Consolidation invariants

1. Never merge the historical #41–#100 stack as an ancestry bundle.
2. Every operational extraction starts from current canonical `main`.
3. Preserve unique code/migrations/tests/evidence before closing a source PR.
4. Reuse exact historical bytes where semantics are correct; repair weaknesses only in forward migrations or newly isolated code.
5. A historical qualified head is evidence for its own revision only.
6. Research ancestry is not accepted merely because operational code is stacked on top of it.
7. No extraction may change strategy/risk economics unless separately scoped and validated.
8. No C1/C2 infrastructure primitive can authorize an ENTRY by itself.
9. Every replacement requires exact-head pre-merge qualification and post-merge `main` qualification.
10. Runtime/operator credentials must not inherit bootstrap/migration/DDL authority.
11. Append-only claims must include physical TRUNCATE resistance, not only row UPDATE/DELETE triggers.
12. C3 must preserve PR #89 at-most-one/no-blind-resubmit semantics.
13. C4 must preserve PR #90 exact-head evidence semantics.
14. A completed deterministic extraction is not connected broker evidence.
15. Profitability remains a separate gate.

## Completed C2A foundation

### C2A0 — durable strategy-free runtime lease

PR #113 extracted the audited v119 runtime-lease primitive onto canonical main without stacked research ancestry. Frozen migration SHA-256:

`c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e`.

### C2A1 — v119 runtime-role least privilege

PR #116 separated bootstrap/migration authority from the long-running PostgreSQL runtime credential. The runtime role is non-owner and lacks database/schema create, DDL and TRUNCATE authority.

### C2A2 — v120 append-only audit hardening

PR #118 preserved historical v120/001 byte-for-byte and added a forward statement-level TRUNCATE guard. Frozen v120/001 SHA-256:

`613a21fba9142f34141cb7d6c81938b175250fb4dbda9bd37726e5627df094cf`.

### C2A3 — strategy-free v120 typed persistence

PR #121 canonicalized only persistence-safe typed authorization/provenance/terminal contracts and PostgreSQL adapters. Strategy selector builders, crypto strategy types, broker/market-data clients, cycle decision logic and order/ARM/mainnet capability remained excluded.

Qualified merge SHA:

`090f34b11a877ce24f8a15a74b296e287aae3918`.

### C2A4 — strategy-free v119 active-excursion CAS persistence

Issue #122 / PR #124 is completed.

Final pre-merge head:

`00a0deffa954c70e44fb70483556746363228535`

Qualified merge SHA:

`043fc3003b055dc1e953854798627048d3f26960`

C2A4 canonicalized only:

- neutral immutable active-excursion state/checkpoint records;
- deterministic canonical JSON and SHA-256 revision identity;
- singleton `ACTIVE` initialize/load/CAS-save/CAS-clear persistence;
- stale revision, cross-entry and tamper rejection;
- existing C2A1 non-owner runtime-role DML boundary.

It deliberately excluded historical strategy, broker, market-data, tracker/observer and order-control code.

Qualification on the exact merge SHA:

- 10/10 exact-head pre-merge workflows SUCCESS;
- 11/11 post-merge push workflows SUCCESS;
- `canonical-security-regression` run `34039454929` — PostgreSQL 16.15, **282 focused passed**, **1124 full passed / 2 dedicated fleet skips**;
- `release-provenance` run `34039454985` — qualification + signed SLSA + signed SBOM PASS;
- frozen canonical-security workflow SHA-256 `8ff9793125804197a828fbec6f56d574882147a62fb73c6201a5cdf6b56823dc`.

## Current: C2B0 — v121 append-only control-journal persistence

Issue #125 is the next bounded implementation unit.

Primary historical source: PR #80 exact head `f40d9dee0baadd254a6fe7425b117be709088f9d`.

Already identified historical blobs:

- `migrations/v121/001_bybit_demo_control_plane.sql` — Git blob `cae1dd432050f235b94d230b2e46c862d38b58c6`;
- `app/execution/bybit_demo_control_plane.py` — Git blob `19c5dc7f4a1f6548b967b6b3ec43ef27d6868135`.

The historical Python file is mixed and cannot be copied wholesale. It imports:

- `BybitDemoOrderRequest`;
- `BybitDemoConnectedPreflightResult` / connected-preflight status;
- and contains `ControlPlaneGuardedBybitDemoClient.place_market_order`.

C2B0 therefore extracts only:

1. neutral immutable `ARM_NEW_ENTRIES` / `HALT_NEW_ENTRIES` event records;
2. deterministic canonical evidence/event hashing;
3. bounded ARM timestamps and preflight-evidence hash consistency as data invariants;
4. append-only v121 PostgreSQL journal;
5. fail-closed latest-event rehydration;
6. separate least-privilege read/append credentials;
7. forward physical TRUNCATE hardening if the preservation audit confirms the historical v121/001 gap.

C2B0 must expose no connected Bybit call, order request/client, order guard, `migrate()` on runtime/operator adapters, strategy/risk economics, session-risk v122, Demo ENTRY or mainnet capability.

## C2B dependency decomposition

### v121 historical weakness to fix forward

Historical `v121/001` has a row trigger rejecting UPDATE/DELETE but no statement-level `BEFORE TRUNCATE` trigger. If byte-preservation audit accepts v121/001 unchanged, C2B0 must add a separate forward hardening migration rather than rewrite history.

### v122 is not safe to copy wholesale

Historical PR #84's `bybit_demo_session_risk_ledger.py` imports `app.strategy.crypto_session_risk.CryptoSessionRiskState` and post-trade accounting types. The PostgreSQL store also exposes `migrate()`.

Therefore v122 requires a later bounded strategy-free persistence extraction with migration authority separated from runtime authority.

### PR #85 belongs after C1

Historical PR #85 adds fixed-egress, credential-backed read-only session-start checks. It is operationally useful but must wait for canonical C1 GET-only broker identity/readiness rather than smuggling broker connectivity into persistence work.

### PR #86 belongs after safe v122 persistence

Historical PR #86's terminal-evidence → risk-commit → checkpoint-ack ordering is valuable crash-safety semantics, but it depends on a canonical session-risk ledger. It follows the strategy-free v122 extraction, not C2B0.

## Remaining work packages after C2B0

### C2B1 — strategy-free v122 session-risk persistence

Expected scope: immutable session ledger/outcome journal, CAS, monotonic peak equity and append-only historical outcomes, with strategy and post-trade builders removed from the persistence contract.

### C2B2 — terminal handoff ordering

Source semantics: PR #86. Preserve terminal-evidence → risk-commit → active-checkpoint ACK ordering and idempotent retry without adding broker mutation.

### C2C — active-trade supervision and controlled recovery

Historical sources: #87/#88. Recovery only returns the system to a known safe state; it cannot create a second ENTRY.

### C1 — canonical GET-only broker identity/readiness

Historical sources include #78/#81/#82. Target read-only environment/account identity, broker server-time/clock health and account/activity evidence with no create/amend/cancel surface.

### C3 — canonical protected Demo entry composition

Source semantics: PR #89. Do not start until canonical C1/C2 foundations **and** strategy-promotion prerequisites pass.

Must preserve:

- fixed egress;
- exact existing ARM state;
- short-lived explicit approval;
- immutable pre-submit authorization/provenance;
- `SUBMIT_STARTED` before broker POST;
- at most one risk-adding ENTRY attempt;
- no blind retry after ambiguous mutation;
- reconciliation before recovery decision;
- protection restoration / policy-authorized reduce-only risk reduction only;
- recovery cannot create a second ENTRY;
- no auto-ARM;
- no mainnet write path.

### C4 — exact-head operational evidence

Required real evidence order from PR #90:

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

### C5 — research lineage isolation

PR #100 remains research-only. Current frozen price-only evidence is negative; derivatives context cannot promote a strategy until authoritative point-in-time acquisition and a separately frozen validation protocol pass.

## Independent blockers

- #103 — `main` branch protection / required review enforcement remains disabled: **P0**.
- #109 — physical TRUNCATE hardening for canonical V107–V109 event tables remains **P1**.
- strategy edge remains **FAIL / NOT PROVEN**.
- connected Demo execution, exact-head broker evidence, soak and production external evidence remain unproven.

## Closure policy

A source PR may be closed as superseded only after:

1. exact source head and changed files are audited;
2. unique executable/migration/evidence content is preserved or explicitly rejected with rationale;
3. replacement starts from current canonical main;
4. replacement exact-head CI passes;
5. replacement is merged with the expected qualified head;
6. post-merge main qualification passes;
7. source PR receives traceability to the replacement.

No historical stack PR should be mass-closed.

## Explicitly not authorized

- bulk merge/cherry-pick of historical PR #93 ancestry;
- strategy promotion from research PRs;
- connected broker access inside C2B0;
- Demo order submission;
- mainnet order capability;
- automatic ARM;
- leverage/capital increase;
- weakening risk controls;
- calling C2A0–C2A4 a production trading runtime;
- claiming profitability.
