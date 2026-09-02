# Stacked PR Consolidation Map

This document is the human-readable execution plan for issue #104. `STACKED_PR_CONSOLIDATION_STATUS.json` is the fail-closed machine contract.

## Why consolidation is mandatory

The repository currently has a long open stack in which production-safety work and research work share ancestry. That ancestry is useful history, but it is not an acceptable production release structure.

At the inventory boundary on 2026-09-02:

- canonical `main`: `6e0c2d08fd9281682e7e2a5771ed3a93ea7df8d9`;
- current operational boundary candidate: PR #93, `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b`;
- current research head: PR #100, `918dbc57c0633c6dc549f1f036d2ae659b289b46`;
- the old stable-main baseline before the P0 repair to PR #93 is approximately 1426 commits;
- PR #93 to PR #100 is another 50 commits dominated by current strategy research.

Therefore neither #93 nor #100 may be merged into `main` as an opaque ancestry bundle.

The required end-state is:

```text
main
  └─ canonical stable fail-closed core
       ├─ extracted operational safety capabilities
       ├─ extracted GET-only broker observability
       └─ no accidental research promotion

operational-candidate/*
  └─ only the minimal remaining change needed for protected Demo proof

research/*
  └─ datasets, diagnostics, hypotheses and frozen evidence
```

## Non-negotiable preservation rules

Before any stacked PR is merged, closed or its branch deleted, re-verify its current head and prove all of the following:

1. changed files have been inventoried;
2. parent/base dependency has been re-verified;
3. unique runtime code is preserved or explicitly superseded;
4. unique migrations are preserved or explicitly superseded;
5. unique tests/workflows are preserved or explicitly superseded;
6. immutable evidence and research records are preserved or archived;
7. a canonical target location is identified;
8. qualification for the extracted unit is defined;
9. no research-only parameter or conclusion is silently promoted into operational behavior.

A later PR existing is not proof that an earlier PR is disposable.

## Range classification

### R1 — PR #41: frozen integration snapshot

**Category:** `FROZEN_INTEGRATION_SNAPSHOT`

PR #41 explicitly declares itself an incubator/integration snapshot and not a production release unit. It must be treated as historical integration ancestry.

Action:

- preserve immutable qualification/history;
- identify any still-unique stable operational files;
- never merge the entire snapshot simply to “catch main up”;
- close only after all unique executable/evidence content is accounted for.

### R2 — PRs #43–#49: early operational core

**Category:** `OPERATIONAL_CORE_EARLY`

These PRs cover reusable production-safety concerns such as operator state, accounting/cash convergence, immutable entry recovery and crash/restart behavior.

Target: canonical operational core.

File-level audit order:

1. persistence/migrations;
2. OMS/execution recovery;
3. portfolio/accounting;
4. supervisor/operator state;
5. tests/fault campaigns;
6. evidence/docs.

Extract in small units from current `main`; do not recreate old version layers if the same behavior can be expressed through the existing canonical interfaces.

### R3 — PRs #50, #51, #53: mainnet read-only observability

**Category:** `MAINNET_READONLY_OBSERVABILITY`

These PRs are valuable because they improve account identity, server-time/health and activity visibility without granting write authority.

Target: canonical GET-only broker observability.

Invariant: extraction must not create, expose or compose mainnet order-write capability.

### R4 — PRs #54–#67: research data and evidence

**Category:** `RESEARCH_DATA_AND_EVIDENCE`

This range contains research/data work: dynamic universe, derivatives/funding history, evidence registries, prospective outcomes, replay/calibration and related analytical infrastructure.

Target: isolated research lineage.

Do not merge this range into the operational core as an ancestry bundle. Reusable point-in-time data-validation primitives may be promoted only through a separate bounded PR whose semantics are independent of a strategy outcome.

### R5 — PR #68: operational bridge with research ancestry

**Category:** `OPERATIONAL_BRIDGE_WITH_RESEARCH_ANCESTRY`

The operator-approved Demo execution bridge is operationally important, but its branch ancestry includes research work.

Target: extract the minimal approval/authorization/execution bridge onto canonical operational foundations.

Required safety checks during extraction:

- no ranking or research outcome automatically authorizes an order;
- current production/Demo selector must independently agree;
- explicit approval remains required;
- no new mainnet write path;
- no automatic ARM;
- no automatic strategy promotion.

### R6 — PRs #69–#73: prospective research evidence

**Category:** `PROSPECTIVE_RESEARCH_EVIDENCE`

Liquidation capture/context/calibration and materialized prospective evidence belong to research governance.

Target: isolated research lineage.

Preserve frozen evidence and acquisition semantics. Promote only generic point-in-time validation components when they can be proven strategy-agnostic.

### R7 — PR #74: operational read-only context

**Category:** `OPERATIONAL_READONLY_CONTEXT`

Target: canonical GET-only broker observability.

This should be reviewed together with R3 to avoid two competing read-only account/context stacks.

### R8 — PRs #75–#93: Demo operational hardening

**Category:** `DEMO_OPERATIONAL_HARDENING`

This is the highest-value extraction range for the next production-capability milestone. It contains later durability, approval, provenance, supervisor, database identity, connectivity, recovery and protected one-shot Demo execution controls.

PR #89 is the key protected one-shot operational composition. Its safety contract must be preserved exactly in substance:

- protected fixed egress;
- existing exact ARM state;
- short-lived explicit operator approval;
- immutable authorization/provenance;
- `SUBMIT_STARTED` before broker mutation;
- at-most-one ENTRY attempt;
- no blind resubmit after ambiguity;
- mandatory post-attempt reconciliation;
- protection restoration or policy-authorized reduce-only recovery;
- recovery cannot create another ENTRY;
- sanitized evidence;
- no new signal, strategy, ranking or risk rule in the operational bridge;
- no auto-ARM;
- no mainnet write path.

PR #90 adds the exact-head evidence chain:

```text
INFRA_READY
  -> SESSION_READY
  -> SUPERVISOR_READY
  -> ARM_PROVEN
  -> DEMO_ENTRY_PROVEN
  -> HALT_PROVEN
  -> RECOVERY_DRILL_PROVEN
```

PRs after #90 add more persistence/identity/control hardening through #93.

Target: decompose and preserve these controls in bounded canonicalization units. Do **not** merge #93 wholesale because its ancestry includes research work that is not independently promoted.

Recommended extraction order for R8:

1. non-mutating environment/account/database identity and readiness;
2. durable supervisor/control state and recovery;
3. approval/authorization/provenance persistence;
4. one-shot order-entry composition with existing OMS invariants;
5. protection reconciliation/recovery;
6. exact-head evidence materialization;
7. protected workflow composition.

Each extracted unit must qualify independently from current `main` before the next unit is accepted.

### R9 — PRs #94–#100: current strategy research and diagnostics

**Category:** `CURRENT_STRATEGY_RESEARCH_AND_DIAGNOSTICS`

This range is evidence, not a release candidate.

It contains the current negative Bybit replay, falsification/OOS work, volatility-feedback diagnostics, target-before-stop analysis, immutable candidate trace and incomplete derivatives-context research.

Current frozen price-only truth must be preserved:

- 102 trades;
- 36 WIN / 11 BE / 55 LOSS;
- approximately `-176.67 USDT` net on `1,000 USDT` reference equity;
- profitability not proven;
- no strategy promotion.

PR #100 may produce a new frozen derivatives-context experiment only after authoritative point-in-time acquisition. It cannot rewrite the frozen negative result by post-hoc retuning.

## Canonicalization work packages

The stack should be reduced through bounded work packages rather than one mega-merge.

### C0 — Inventory and proof of preservation

No behavior change.

For every PR:

- current head SHA;
- current base/head relationship;
- changed file list;
- migrations;
- runtime modules;
- tests/workflows;
- immutable evidence;
- downstream duplicate/supersession status.

Only after C0 may an individual PR receive a proposed `PRESERVE / EXTRACT / SUPERSEDE / ARCHIVE / CLOSE` decision.

### C1 — Canonical GET-only broker observability

Combine the minimal non-mutating value from R3 and R7 against current `main`.

Pass criteria:

- no order-write construction;
- no production credential escalation;
- account/environment/time/health identity is explicit;
- read-only failures fail closed for trading readiness;
- tests prove write isolation.

### C2 — Canonical durable operational control plane

Extract reusable supervisor/operator/approval persistence from R2/R8.

Pass criteria:

- durable restart state;
- HALT/PAUSE/READ_ONLY semantics preserved;
- stale/contradictory state fails closed;
- no broker mutation yet.

### C3 — Canonical protected Demo entry composition

Extract the minimal #89 semantics only after C1/C2 are qualified.

Pass criteria:

- exactly one allowed Demo ENTRY attempt;
- pre-submit persistence and exact approval binding;
- no automatic retry/second entry;
- broker ambiguity resolved by GET/reconciliation;
- no strategy/risk-rule changes;
- no mainnet write path.

A successful deterministic C3 is still `DEMO_UNPROVEN` until a protected connected execution is run.

### C4 — Exact-head operational evidence

Extract #90-style chain after the operational runtime is canonical.

Pass criteria:

- one exact release SHA;
- ordered, non-overlapping evidence;
- source hashes;
- workflow identity;
- real Demo evidence required for `DEMO_ENTRY_PROVEN`;
- contradictory/missing evidence fails closed.

### C5 — Research lineage isolation

Retarget or rebuild the active research line from an explicitly research-safe base so it can evolve without dragging operational release ancestry.

No research refactor may change the frozen outcome records.

## What is explicitly not being done in this phase

- no branch deletion;
- no closure of #41–#100 based on range classification alone;
- no bulk merge into `main`;
- no strategy parameter change;
- no leverage/capital/risk increase;
- no Demo order;
- no mainnet write;
- no claim that #93 is production-ready;
- no claim that #100 is profitable.

## Next file-level audit order

To maximize safety/value and minimize wasted archaeology, inspect in this order:

1. #89–#93 — identify the minimal latest operational controls and direct dependencies;
2. #75–#88 — trace what #89–#93 actually require versus what is superseded;
3. #68 and #74 — isolate bridges/read-only context from research ancestry;
4. #43–#53 — identify stable reusable operational primitives not already superseded;
5. #94–#100 — preserve research evidence and identify clean research-only base requirements;
6. #54–#73 — retain only data/research primitives not already superseded;
7. #41 — final snapshot closure analysis after all unique content is accounted for.

The immediate objective is not fewer PR numbers. The objective is one understandable, independently qualified production-capable code path with research kept separate and no loss of safety evidence.
