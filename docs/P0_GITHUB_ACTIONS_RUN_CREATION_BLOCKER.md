# P0 — GitHub Actions is not creating qualification runs

## Status

**OPEN / RELEASE BLOCKER**

This incident is external to the Bybit Demo trading logic but blocks promotion of the persistent supervisor and controlled lease-recovery stack because exact-head CI evidence cannot currently be produced.

Do not interpret an empty Actions run list as a green build.

## Last proven green baseline

The last exact-head qualified Demo runtime layer is PR #86:

```text
700e59e0b67329ea7df00bbd78aebd6ddfdba334
```

It completed all eight registered qualification workflows successfully, including full regression and schema99 stacked coverage.

## Reproduction

### PR #87 — persistent supervisor

Actions created **zero** workflow runs/check suites after:

- initial PR creation;
- multiple same-repository `synchronize` commits;
- PR close + reopen;
- an explicit `push` trigger added for `agent/bybit-demo-persistent-worker`;
- another push to that exact branch.

The repository Actions run query for that branch returned zero runs.

### PR #88 — controlled runtime-lease recovery / v123

The same behavior reproduces independently:

- PR created from `agent/bybit-demo-controlled-lease-recovery`;
- multiple commits pushed after PR creation;
- dedicated recovery workflow explicitly subscribes to both PR changes and direct pushes to the feature branch;
- repository Actions run query for that branch still returns zero runs.

This rules out one malformed workflow/path filter as the sole cause.

## What has already been ruled out

- PR merge conflict: both feature PRs have recalculated as mergeable during diagnosis.
- Path filter mismatch: existing `stable-core-quality` watches `app/execution/**` / `app/marketdata/**`, which #87 changes directly.
- New-workflow-only behavior: an already-existing workflow should also have matched #87.
- GitHub-wide Actions outage: public GitHub Status reported Actions operational during reproduction.
- One branch-specific failure: reproduced on two distinct stacked feature branches.

The available GitHub connector can inspect historical runs/jobs/logs and rerun existing jobs, but it does not expose repository Actions enablement/policy/billing controls or a generic workflow-dispatch action. Therefore repository-level configuration cannot be truthfully repaired from this agent session.

## Manual repository / organization checks

Check these controls before changing product code again:

1. **Repository → Settings → Actions → General**
   - Actions enabled for the repository;
   - organization policy permits the workflows/actions used here;
   - no unexpected restriction to selected actions/workflows.
2. **Organization Actions policy**
   - repository not newly excluded by an org-level rule;
   - pinned `actions/checkout`, `actions/setup-python`, `actions/upload-artifact` remain permitted.
3. **Billing / Actions usage / spending limits** for private-repository runner minutes where applicable.
4. **Workflow state** in the Actions UI
   - `stable-core-quality` and other existing workflows not manually disabled;
   - no repository-wide Actions disablement.
5. **Rulesets / branch protection / required workflows**
   - no recent policy change suppressing same-repository stacked PR workflow creation.
6. **Audit log** around the time after #86 completed and before #87 was opened
   - Actions disabled/enabled event;
   - org/repo policy change;
   - billing or permission change.

Do not weaken branch protection or remove safety tests merely to make the PR mergeable.

## Recovery / exit criteria

The incident is resolved only when a new commit on the current feature stack creates fresh GitHub Actions runs again.

For #87, require the dedicated persistent-supervisor qualification to execute PostgreSQL 16 targeted proof, full `pytest -q`, Ruff, Bandit, and compile.

For #88, require the dedicated runtime-lease-recovery qualification to execute isolated PostgreSQL 16 recovery/control/bootstrap proofs, CLI sanitization tests, full regression, Ruff, Bandit, and compile.

Then record the **exact qualified head SHA** and update the PR bodies. Historical #86 success is not evidence for new #87/#88 code.

## Connected-operation boundary

Restoring CI is still not a connected Bybit Demo PASS.

After code qualification, the separate protected self-hosted operational gates must still prove:

- v119-v123 production database readiness;
- connected read-only Demo account/database reconciliation;
- least-privilege Demo trading credential posture;
- initialized v122 risk session;
- one-cycle persistent-supervisor smoke;
- operator-approved entry and full terminal accounting/recovery lifecycle.

Mainnet remains read-only throughout.
