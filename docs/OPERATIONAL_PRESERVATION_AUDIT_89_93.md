# Operational Preservation Audit — PRs #89–#93

Companion machine record: `OPERATIONAL_PRESERVATION_AUDIT_89_93.json`.

This is the first completed file-level C0 sub-audit under issue #104. It does not authorize merging or closing any audited PR.

## Exact current chain verified on 2026-09-02

| PR | Current exact head | Exact base SHA | Changed files | Capability |
|---|---|---|---:|---|
| #89 | `fe124d84c490055e2fbe5e711ab2c61f37d0611a` | `d40e3e9b6740896cd5317c01eb9f482cb238b53f` | 17 | protected one-shot Demo entry |
| #90 | `16fe096c2f2f4dba46d35f36508b132168773965` | #89 exact head | 22 | exact-head operational evidence |
| #91 | `22fc150b14d52fa15007554b4d2bc4996b72fdc1` | #90 exact head | 19 | GET-only same-account identity |
| #92 | `8efc89e574bfd7e0edfec9fc3ff022e25f4cd809` | #91 exact head | 17 | cross-dispatch operational-zone binding |
| #93 | `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b` | #92 exact head | 23 | immutable logical operational DB identity v124 |

The chain itself is clean and contiguous. That does **not** mean commit-order replay is the correct canonicalization method.

## #89 — preserve protected one-shot entry semantics

Primary owned surfaces include:

- `app/execution/bybit_demo_approved_runtime.py`;
- `app/execution/bybit_demo_operational_entry.py`;
- `app/execution/bybit_demo_operational_reconciliation.py`;
- `app/execution/bybit_demo_operator_approval.py`;
- `.github/workflows/bybit-operator-approved-demo-execution.yml`;
- approval/entry tools and dedicated tests.

Required preserved invariants:

- existing ARM only; no auto-ARM;
- explicit short-lived approval;
- immutable approval/authorization provenance;
- `SUBMIT_STARTED` before broker POST;
- at most one ENTRY attempt;
- no blind retry after ambiguous mutation;
- mandatory post-attempt reconciliation;
- recovery may protect/reduce/flatten according to policy but cannot create a second ENTRY;
- no strategy/ranking/risk-rule retuning inside the operational bridge;
- no mainnet write path.

However, the canonical #89-equivalent must consume the later identity gates from #91–#93 before an order-capable client is composed.

## #90 — preserve exact-head evidence as C4, not as runtime activation

Primary owned surfaces include:

- `app/execution/bybit_demo_operational_release_checkpoint_binding.py`;
- `app/execution/bybit_demo_operational_release_evidence.py`;
- `.github/workflows/bybit-demo-operational-release-evidence.yml`;
- `tools/assemble_bybit_demo_operational_release_evidence.py`.

Required stage sequence remains:

```text
INFRA_READY
  -> SESSION_READY
  -> SUPERVISOR_READY
  -> ARM_PROVEN
  -> DEMO_ENTRY_PROVEN
  -> HALT_PROVEN
  -> RECOVERY_DRILL_PROVEN
```

The canonical assembler must ingest the final current source-artifact formats. Therefore it should be extracted after the identity/readiness and entry slices, not before them.

## #91 — extract before order capability

Primary owned surfaces:

- `app/execution/bybit_demo_same_account.py`;
- `app/execution/bybit_demo_operational_release_account_binding.py`;
- `.github/workflows/bybit-demo-same-account.yml`;
- `tools/probe_bybit_demo_same_account.py`.

This is a strong candidate for an early bounded canonical PR because the core proof is GET-only and closes a dangerous account-mismatch class:

- read-only key and trading key remain different API keys;
- both must resolve to the same exact Demo account;
- no order-capable client is constructed before the identity proof passes;
- raw UIDs/keys/secrets are not serialized.

## #92 — cross-dispatch operational-zone binding

Primary owned surfaces:

- `app/execution/bybit_demo_operational_zone_binding.py`;
- `app/execution/bybit_demo_operational_release_zone_binding.py`;
- `.github/workflows/bybit-demo-operational-zone-binding.yml`;
- `tools/probe_bybit_demo_operational_zone_binding.py`.

The zone proof prevents an apparently valid evidence chain from silently mixing different protected DB/account resources between manual runs. It is evidence-only and should remain unable to submit/manage an order.

## #93 — logical DB identity v124

Primary owned surfaces:

- `migrations/v124/001_bybit_demo_operational_database_identity.sql`;
- `app/execution/bybit_demo_operational_database_identity.py`;
- `app/execution/bybit_demo_activation_readiness_v124.py`;
- `app/execution/bybit_demo_operational_release_logical_db_binding.py`;
- `.github/workflows/bybit-demo-logical-db-identity-v124.yml`.

This closes a real lineage ambiguity that endpoint/database-name matching cannot solve: an independently initialized database at the same later endpoint must not masquerade as the previous operational database.

Canonical preservation must retain:

- exactly one immutable logical operational DB identity;
- valid backup/restore lineage preserves identity;
- independently initialized DB receives a new identity;
- UPDATE/DELETE/TRUNCATE protection;
- raw UUID not serialized;
- current readiness/evidence rejects legacy identity formats.

## Correct canonicalization order

The important conclusion is that source PR order and target capability order differ.

Required target sequence:

1. **C2 prerequisites from #75–#88** — identify and canonicalize the durable session/supervisor/ARM/recovery dependencies on which #89–#93 rely.
2. **C1 current identity/readiness** — extract #91 -> #92 -> #93 capability semantics.
3. **C3 protected one-shot entry** — extract #89 semantics but wire them to the current C1 identity/readiness contracts.
4. **C4 exact-head evidence** — extract #90 assembler against the final canonical artifact formats.

This avoids a subtle but serious regression: cherry-picking #89 first would recreate an older entry composition that predates the same-account, cross-dispatch zone and logical database identity gates.

## Current preservation decision

For all five PRs:

- merge wholesale: **NO**;
- cherry-pick wholesale: **NO**;
- close: **NO**;
- delete branch: **NO**;
- preserve exact source/evidence: **YES**;
- canonicalize by bounded capability slices: **YES**.

## Next audit

PRs #75–#88 are now the critical dependency set. The next C0 pass must determine which of their control-plane, session-risk, ARM/HALT, persistent-supervisor, protection and runtime-lease recovery components are still required by #89–#93 and which intermediate version layers are fully superseded.
