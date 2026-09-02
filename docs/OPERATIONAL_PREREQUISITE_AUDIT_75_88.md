# Operational Prerequisite Audit — PRs #75–#88

Companion machine record: `OPERATIONAL_PREREQUISITE_AUDIT_75_88.json`.

This audit answers one narrow question before any operational extraction begins:

> Which controls from PRs #75–#88 are actually required by the current #89–#93 operational boundary, and which intermediate version wrappers should be superseded by their final current forms rather than replayed into canonical `main`?

No PR in this range is authorized for wholesale merge, cherry-pick, closure, or branch deletion by this document.

## Summary conclusion

The useful operational semantics are substantial and should be preserved, but the historical commit/version ladder should **not** be replayed mechanically.

The minimal capability model is:

```text
C2A durable runtime/audit foundation
  v119 lease/checkpoint
  v120 append-only approval/provenance/terminal evidence

C2B control/session-risk
  v121 ARM/HALT
  v122 session-risk + one-time session start
  terminal evidence -> risk -> checkpoint commit ordering

C2C active-trade supervision/recovery
  persistent supervisor with no new-entry authority
  v123 explicit audited lease recovery

C1A connected identity/readiness
  read-only account/DB preflight
  GET-only trading-key metadata proof
  fixed-egress trust zone

C1B final bootstrap/readiness
  final v119-v124 bootstrap/readiness from #93

C3A immutable approval lineage
  outcome-free exact-symbol operator authorization
```

The v119→v124 migration lineage must be preserved, but intermediate bootstrap/readiness wrappers from #79/#83 should not become the final canonical interface because #93 already strengthens those semantics through v124.

## PR #75 — immutable approval lineage

Current head: `755b1dc2d2330b53b3ecc0d32db1cc4eb5e21814`.

Required semantics:

- outcome-free authorization lineage is persisted before mutation;
- approval is exact-symbol; no ranked fallback after approval;
- deterministic entry identity links approval to the eventual order;
- after restart/late safety block, already consumed authorization becomes recovery-only and cannot authorize resubmit;
- terminal fee/funding/all-in-P&L attribution remains diagnostics-only and cannot feed online ranking or auto-retuning.

This is a required C3 prerequisite, not strategy promotion logic.

## PR #76 — v119 durable runtime lease/checkpoint

Current head: `84b9965e5128562c49620c6164afe5f43ca343f2`.

Required semantics:

- PostgreSQL singleton single-writer runtime lease;
- no TTL-based stale takeover;
- no automatic lease stealing;
- orphaned lease blocks new ENTRY until controlled verified recovery;
- PostgreSQL active excursion checkpoint with CAS semantics;
- deterministic revision identity across persistence backends;
- concurrent second active-trade initialization rejected.

This is the base of C2A.

## PR #77 — v120 append-only operational audit state

Current head: `6d557dd4bdb555b68789db247b9a112b962e2481`.

Required PostgreSQL records:

- approval authorization;
- protected-entry provenance;
- fully reconciled terminal evidence.

The final system must preserve canonical record identity and append-only semantics. UPDATE/DELETE and privilege bypasses remain forbidden. These stores contain no order-submit method.

## PR #78 — connected read-only Demo preflight

Current head: `09b58db5b34252473c670fa6b600c45df0a33ce0`.

Required semantics:

- a separate Demo read-only credential is authenticated and must actually report `readOnly=1`;
- wallet/account/open positions/open orders are read only;
- connected exchange position is reconciled against durable checkpoint identity, symbol, side, quantity and average entry;
- checkpoint entry `orderLinkId` must be present in execution history;
- pending orders fail closed unless they are compatible reduce-only protection for the canonical existing position;
- multiple/orphan/mismatched positions, schema failure or unsafe state block readiness;
- outputs are sanitized and non-trade-actionable.

This remains a C1 readiness gate, not broker write authority.

## PR #79 — explicit PostgreSQL bootstrap semantics

Current head: `ca08bc3278d0dda8916d81edaf27b6dab738b6a6`.

Important semantics introduced here must survive:

- explicit `verify` vs `apply` modes;
- exact human confirmation before DDL mutation;
- advisory locking against concurrent bootstrap;
- deterministic ordered migration application;
- migration SHA-256 fingerprints;
- post-apply verification;
- idempotent re-apply qualification.

However, its old v119/v120 implementation should **not** be replayed as the final canonical bootstrap. The target implementation is the final v119-v124 form from #93.

## PR #80 — v121 ARM/HALT control plane

Current head: `f40d9dee0baadd254a6fe7425b117be709088f9d`.

Required semantics:

- append-only PostgreSQL operator control journal;
- absent/invalid/expired authority means HALT;
- ARM is short-lived and requires fresh clean connected readiness while runtime is idle;
- ARM acquisition and runtime lease obey coordinated locking;
- the approved runtime checks ARM under the canonical lease and again immediately before any non-reduce-only submit;
- a late HALT blocks submit after authorization persistence, turning that authorization into recovery-only evidence;
- HALT blocks new exposure but not required protection/reduce-only recovery of an already-open trade.

PR #88 later hardens v121 journal integrity; canonicalization must use the hardened final semantics.

## PR #81 — GET-only least-privilege trading credential proof

Current head: `2e6671dc696e18a381764d5e9a579b78c117997e`.

This gate inspects the dedicated Demo trading key **without placing an order**.

Required shape includes:

- inspected key is write-enabled (`readOnly=0`) but used here only for GET metadata;
- concrete IP binding;
- personal key / UTA contract;
- ContractTrade permissions limited to the exact required order/position scope;
- other permission categories empty;
- key namespace differs from Demo read-only and mainnet read-only credentials;
- sanitized result contains no raw key, secret, UID, IP, permission map or fingerprint.

Passing this gate proves credential metadata only, never trade authorization.

## PR #82 — protected fixed-egress operational zone

Current head: `d088d040c23eab778a56a93f45488cd0ff38806e`.

Required semantics:

- operational jobs run only in the protected `self-hosted, bybit-demo` environment;
- stable fixed outbound IP is part of the trust boundary;
- Demo read-only key must have a concrete compatible IP binding;
- malformed/empty/wildcard-only bindings fail closed;
- the fixed-egress wrapper rechecks readiness before delegating to durable ARM;
- PR qualification remains credential-free.

The existence of the code does not prove that the actual protected runner/environment/fixed egress exists.

## PR #83 — activation readiness semantics

Current head: `3a67672db6609f3f4d3399eccd7f5bbec7a33c2b`.

Required semantics:

- one bounded readiness assembly combines DB verification, connected read-only preflight, trading-credential metadata proof and control-plane status;
- exact Git SHA and exact sanitized source-evidence hashes are bound into the manifest;
- readiness requires HALTED state;
- readiness never ARMs, approves or places a trade.

Its pre-v124 wrapper should not be the final canonical implementation. The final v124 readiness form from #93 supersedes it while preserving these semantics.

## PR #84 — v122 restart-safe session risk

Current head: `bdcdf7189b56b494e14ac746ad9b867c108dcd30`.

Required semantics:

- opening equity immutable;
- equity high-water monotonic;
- terminal all-in trade outcomes append-only;
- active checkpoint and outcome journal cross-verified on load;
- every save uses CAS revision;
- stale revision fails closed;
- already reconciled economics cannot disappear or mutate;
- no automatic reset/clear/takeover/imported-history initialization.

This prevents restart from resetting drawdown/loss history.

## PR #85 — one-time session start

Current head: `067adc55845556c772f1e9c3281a1ae995a67ada`.

Required semantics:

- explicit one-time initialization command and confirmation;
- exact code SHA/operator/reason captured;
- only DB + read-only exchange credential needed; no trading credential;
- requires v121 HALT, no runtime lease, no active checkpoint, no existing v122 session and flat exchange state;
- opening equity comes from a fresh authenticated wallet read;
- position/order state is rechecked immediately before singleton insertion under database locks;
- no reset/reinitialize/takeover path.

## PR #86 — crash-safe terminal risk commit

Current head: `700e59e0b67329ea7df00bbd78aebd6ddfdba334`.

Required terminal sequence:

```text
immutable terminal evidence
  -> v122 fully reconciled session-risk economics
  -> exact active checkpoint ACK/clear
```

Failure semantics are important:

- evidence failure moves nothing;
- risk commit failure leaves terminal evidence durable but checkpoint active;
- checkpoint ACK failure leaves both durable records and allows idempotent retry;
- conflicting economics for one entry identity fail closed.

The runtime must never invent opening equity or silently initialize session risk.

## PR #87 — persistent active-trade supervisor

Current head: `d8e937c70e046afc9abe39732511e824fca03b86`.

Required semantics:

- with no active checkpoint the supervisor is IDLE;
- it has no selector, approval input or new-entry authority;
- a hard-block entry executor prevents a race from falling through into exposure creation;
- it observes real Demo wallet equity into durable session risk;
- terminal handoff cannot create same-invocation replacement entry;
- active-trade management can tighten protection and perform policy-authorized reduce-only close/flatten only for existing Demo exposure;
- fresh broker state is checked before reduce-only action;
- ambiguous close is never blindly retried;
- one-shot and loop service modes fail closed and stop on unknown management state.

## PR #88 — v123 audited lease recovery and ancestry drift

Current head: `d40e3e9b6740896cd5317c01eb9f482cb238b53f`.

Required recovery semantics:

- immutable v123 recovery history blocks UPDATE/DELETE/TRUNCATE;
- raw lease owner token is never persisted in evidence; only its SHA-256 identity;
- recovery requires a canonical verified v121 HALT event, exact inspected owner fingerprint, operator/reason, external process-stop evidence and exact confirmation phrase;
- lock order is lease first, control second, matching ARM ordering;
- the transaction deletes only the exact lease owner read under lock;
- active checkpoint remains intact;
- old-fingerprint retry after lost response returns `ALREADY_RECOVERED` from immutable audit history;
- recovery does not ARM, reset v122, clear active trade state, infer process death from age/PID/heartbeat, or call the exchange.

PR #88 also hardens v121:

- no TRUNCATE of the control journal;
- canonical control event identities are recomputed and verified;
- forged/hash-mismatched control rows cannot grant authority.

### Important ancestry hazard

The current PR metadata is not a perfect linear chain at #87→#88.

- current #87 head: `d8e937c70e046afc9abe39732511e824fca03b86`;
- #88 base/merge-base: `6cf3891d3f953e648dc5582dbc604eaa87dd7488`;
- comparing current #87 head to #88 head reports `behind_by=2`, `ahead_by=30`.

The two late #87 commits were cleanup fixes after the merge-base. The instrument file at current #87 and #88 has the identical blob SHA `361cd209ef97bdbf4d3561ca3b39a3025fa9648c`, so that cleanup is semantically present despite divergent ancestry. The supervisor CLI differs because #88 also updates the required schema contract from v122 to v123 while reflecting the cleanup.

Therefore canonical extraction must compare final file semantics and tests. It must **not** assume that the textual phrase “stacked on #87” proves the current #87 head is an ancestor.

## Migration lineage to preserve

The final operational database lineage required by the current boundary is:

```text
v119  #76  runtime lease + active checkpoint
v120  #77  append-only approval/provenance/terminal evidence
v121  #80  ARM/HALT control journal
            hardened by #88
v122  #84  restart-safe session risk
v123  #88  audited runtime-lease recovery + v121 no-TRUNCATE hardening
v124  #93  immutable logical operational database identity
```

The canonical bootstrap should apply/verify the final ordered v119-v124 contract once extracted. Do not ship sequential legacy operator interfaces for v119-v120, then v119-v121, then v119-v122, etc.

## Canonical capability slices

### C2A — durable runtime/audit foundation

Source semantics: #76, #77.

This is the logical first executable extraction target after checking whether current `main` already contains equivalent capability.

### C2B — control and session-risk

Source semantics: #80, #84, #85, #86.

Depends on C2A.

### C2C — active-trade supervision and controlled recovery

Source semantics: #87, #88.

Depends on C2A/C2B and must preserve the #87/#88 ancestry-drift resolution explicitly.

### C1A — connected identity/readiness prerequisites

Source semantics: #78, #81, #82.

May be canonicalized independently of order mutation as GET-only/readiness infrastructure.

### C1B — final bootstrap/readiness

Source semantics were introduced in #79/#83 but final target comes from #93 v124 forms.

### C3A — approval lineage prerequisite

Source semantics: #75.

Must remain outcome-free and independent from automatic ranking/strategy promotion.

## Next gate

Before copying any of these files to canonical `main`, compare current `main` against the required C2A/C2B/C2C/C1A semantics. Existing equivalent code must be reused rather than duplicated.

The first proposed executable extraction, only if the main-equivalence audit confirms it is missing, is:

`C2A — v119 runtime lease/checkpoint + v120 append-only approval/provenance/terminal evidence`

That extraction must be a new bounded PR from current `main`, with no research ancestry and no order-write activation.
