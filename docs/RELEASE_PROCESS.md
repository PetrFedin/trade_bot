# Release Process

## Principles

1. `main` is the product integration branch; schema branches are historical implementation layers, not separate products.
2. No release is qualified from documentation alone. The exact release head must pass all mandatory automated gates.
3. Paper and live environments are separate trust domains. A Paper PASS never authorizes live order routing.
4. Ambiguous broker mutations are reconciled by reads; blind mutation retries are forbidden.
5. A failed reconciliation, unknown broker state, or unexplained cash/position delta fails closed.

## Required release sequence

1. Create a focused branch from the current product integration baseline.
2. Keep all user-visible/runtime changes and their tests in the same PR.
3. Run focused qualification gates and the complete stacked regression suite.
4. Require CODEOWNERS review once repository protection is enabled.
5. Record the exact head SHA and E2E matrix state.
6. Merge only after all mandatory gates are green.
7. Produce a signed annotated tag and immutable release notes after signing infrastructure is connected.
8. Build artifacts from the tagged commit only.
9. Generate SBOM/provenance and dependency/security reports before production promotion.

## Mandatory deterministic gates

- paper execution safety regression;
- trading core E2E;
- durable OMS + reconciliation E2E;
- market-data + operational-risk qualification;
- complete pytest regression;
- compile validation;
- dependency installation/check.

## Mandatory Paper-environment gates before Paper readiness >=8/10

- credentialed Alpaca Paper REST read succeeds;
- trade_updates WebSocket authentication/listen succeeds;
- controlled paper submit/replace/cancel/fill drill succeeds with deterministic client IDs;
- ambiguous mutation scenarios recover through read-only reconciliation;
- process/database/network restart tests produce no duplicate broker mutation;
- broker cash and positions reconcile with the internal ledger;
- 14–30 consecutive days of soak evidence satisfy agreed SLOs.

## Mandatory live gates

Live routing remains disabled until all E2E matrix categories are >=8/10 and there is independent approval of a tiny-capital pilot. Live enablement must be a separate release decision, not a configuration side effect of a Paper release.

## Known release debt

- the distribution still uses the historical Schema 108 package identity because the V108 release-identity audit currently asserts it;
- the stacked Schema PR history has not yet been consolidated onto `main`;
- a dependency lock, signed tag flow, SBOM/provenance gate, CodeQL/security gate, and branch-protection enforcement still need to be completed.

Those items are release blockers for an >=8/10 release-process score, not documentation-only TODOs.
