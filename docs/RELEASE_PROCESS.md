# Release Process

## Principles

1. `main` is the canonical stable integration branch. Historical schema/platform branches and the later stacked Bybit/research PRs are implementation history or candidate work, not separate products.
2. No release is qualified from documentation, test count or code presence alone. The exact release head must pass the mandatory gates applicable to the claimed capability.
3. Deterministic/code qualification, connected broker qualification, Demo qualification, live qualification and profitability qualification are distinct states and may not substitute for one another.
4. Paper/Demo and production/mainnet environments are separate trust domains. A Paper/Demo PASS never authorizes live order routing.
5. Ambiguous broker mutations are reconciled by reads; blind mutation retries are forbidden.
6. A failed reconciliation, unknown broker state, unexplained cash/position delta or missing protection fails closed and prevents new ENTRY.
7. Recovery exists to restore a known safe state. It must not create a replacement ENTRY.
8. Strategy promotion requires separately frozen, cost-adjusted OOS evidence. Engineering readiness cannot compensate for negative or inconclusive trading expectancy.

## Current identity boundary

Use `CURRENT_SYSTEM_STATUS.json` for the machine-readable current identities. As observed on 2026-09-02:

- canonical `main` last fully re-qualified engineering baseline: `0bc1e309e1b268756023088b6e440db30cb4ddab`;
- current operational boundary candidate for consolidation: PR #93, exact head `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b`, status `CODE_QUALIFIED_DEMO_UNPROVEN`;
- active research head: PR #100, exact head `918dbc57c0633c6dc549f1f036d2ae659b289b46`, status `RESEARCH_ONLY`;
- current strategy profitability: **not proven**;
- live/mainnet routing: **fail-closed**.

The operational and research heads are intentionally not treated as interchangeable release candidates.

## Required source-change sequence

1. Branch from the exact current canonical baseline unless a documented stacked dependency requires another base.
2. Keep a PR bounded to one purpose; do not mix strategy research with execution-authority changes.
3. Update tests and authoritative documentation in the same change when behavior, contracts or readiness claims change.
4. Run focused qualification first, then all mandatory full-regression/security/release gates triggered by the affected surfaces.
5. Record the exact head SHA and distinguish mocked/local/connected/Demo/production evidence.
6. Treat deterministic failures and newly exposed database/integration failures as blockers; do not rerun-until-green or hide them with environment-based skips.
7. Merge only after the exact PR head satisfies its required gates.
8. Re-verify applicable push workflows on canonical `main` after merge where the claim depends on the merged state.
9. Never infer external broker or profitability readiness from a green deterministic merge.

## Mandatory deterministic engineering gates

At minimum for release-critical changes:

- stable trading-core/product-composition regression;
- paper execution safety and order-lifecycle regression;
- durable OMS, mutation and reconciliation tests;
- SQLite and PostgreSQL persistence/fault qualification where applicable;
- risk/portfolio/accounting regression;
- market-data and strategy-contract qualification for affected research/runtime surfaces;
- full deterministic pytest regression;
- canonical security migrations and their idempotency where required;
- compile/static validation;
- hash-locked dependency installation and freshness checks;
- dependency vulnerability/security checks;
- GitHub Action supply-chain policy;
- fail-closed live-state assertions.

The canonical `main` engineering baseline repaired on 2026-09-02 executes the ordinary full regression with PostgreSQL enabled rather than suppressing PostgreSQL tests. The two fleet-deployment integration tests that require `ASTRA_TEST_FLEET_DEPLOYMENT_DSN` remain owned by the dedicated canonical deployment regression instead of being represented as generic full-regression PASS.

## Release artifact and supply-chain gates

Repository-side controls already implemented include:

- SHA-256 hash-locked dependency graph and lock-freshness verification;
- exact approved GitHub Action commit pins for operational workflows;
- Ruff/Bandit/dependency audit/security qualification;
- wheel/sdist release artifacts;
- SPDX SBOM generation;
- release-manifest generation;
- SLSA build provenance and signed SBOM attestations on trusted `main`/tag flows;
- SHA-256 transfer verification before attestation;
- machine-readable artifact release and rollback ownership.

These existing controls must be retained. They are not future TODOs.

A versioned release additionally requires:

1. the candidate is reachable from canonical `main`;
2. any `v*` tag exactly matches the project version contract;
3. the tag/artifacts are immutable rather than replaced in place;
4. governance/provenance/SBOM/security gates succeed on the relevant trusted release flow;
5. the exact artifact digest is retained in release evidence.

## Server-side governance gate

Repository-side governance cannot replace GitHub server-side enforcement.

Current verified state is `VERIFIED_DISABLED` for `main` protection and required status-check enforcement. Therefore production/live promotion is blocked even though repository-side validators exist.

Before production relies on GitHub enforcement, server-side configuration must be enabled and independently re-read, including the required PR/review/status-check policy, force-push/deletion restrictions and protected production environment. This work is tracked in issue #103.

An independent live approver also remains unassigned. Artifact release ownership is not equivalent to independent live approval.

## External Paper / Demo gates

A connected broker capability may be called qualified only when a real credential-backed run produces current evidence. A workflow completing safely while broker steps are skipped for missing credentials is not broker qualification.

For Bybit, the protected operational path must preserve the existing safety boundaries:

- protected fixed-egress execution environment;
- exact qualified operational head;
- GET-only least-privilege credential/readiness preflight;
- persistent supervisor/control state;
- existing exact ARM event/state;
- short-lived explicit operator approval;
- immutable authorization/provenance;
- durable `SUBMIT_STARTED` before mutation;
- at-most-one allowed ENTRY attempt;
- no blind retry after ambiguous broker mutation;
- mandatory post-attempt broker reconciliation;
- verified position protection or policy-authorized reduce-only recovery;
- HALT and controlled recovery evidence;
- sanitized evidence with no secret leakage.

A real protected Demo ENTRY has not yet been proven through the current operational path, so this gate remains open.

## Exact-head operational evidence gate

Operational promotion requires the full evidence chain on one exact SHA:

```text
INFRA_READY
  -> SESSION_READY
  -> SUPERVISOR_READY
  -> ARM_PROVEN
  -> DEMO_ENTRY_PROVEN
  -> HALT_PROVEN
  -> RECOVERY_DRILL_PROVEN
```

Evidence from different heads may not be assembled into a false release PASS. Each source artifact must be hash-bound and the chain must reject missing, contradictory, out-of-order or unsafe states.

## Strategy promotion gate

A strategy is not promoted merely because it produces signals or because the execution platform is safe.

Before `RESEARCH -> DEMO_CANDIDATE`, the strategy must have frozen evidence supporting, at minimum:

- no material look-ahead / point-in-time violation;
- realistic fees, spread, slippage and funding where applicable;
- positive net expectancy on an untouched/OOS protocol;
- acceptable drawdown/tail behavior;
- adequate trade/sample/regime coverage;
- no unacceptable concentration in one symbol/event/regime;
- reasonable parameter/cost sensitivity;
- frozen strategy/data/cost-model identity;
- no outcome-driven retuning after the final holdout is opened.

The latest frozen Bybit price-only replay is negative, so the current strategy status remains `PROFITABILITY_NOT_PROVEN` and promotion is forbidden from that evidence. Incomplete derivatives-context research may become a new frozen experiment; it may not retroactively rewrite the existing result.

## Long connected soak and failure qualification

After first successful protected Demo execution, do not move directly to mainnet.

A connected soak must cover meaningful operational cycles and exercise/recover from conditions such as:

- market-data and WebSocket reconnects;
- REST/rate-limit failures;
- process restart;
- database reconnect/restart;
- stale data and clock-skew protection;
- partial fills;
- duplicate/out-of-order broker events;
- ambiguous broker responses;
- reconciliation mismatch;
- protection verification/restoration;
- HALT/recovery behavior.

Hard safety outcomes include zero unintended duplicate ENTRY, zero unresolved orphan exposure at release gate, zero silent secret leakage and no silent exception that can increase exposure.

## Production/mainnet gate

Mainnet ENTRY remains forbidden until all separately required evidence is PASS, including:

- software correctness;
- data integrity;
- cost-adjusted strategy evidence;
- connected Demo execution;
- exact-head operational evidence chain;
- connected soak/failure recovery;
- server-side governance;
- security/least privilege;
- reproducible deployment and rollback;
- backup/restore qualification;
- observability/alerts/runbooks;
- independent live approval.

The first production step, when eventually authorized, must be a separately approved tiny-capital pilot with hard notional/drawdown limits and no automatic capital scaling.

## Current release debt / blockers

The following remain real blockers rather than documentation TODOs:

- GitHub `main` branch protection and required-review/status enforcement are verified disabled (#103);
- the very large stacked PR history has not been consolidated into canonical core, operational candidate and isolated research (#104);
- the latest frozen Bybit strategy evidence is negative and a positive frozen cost-adjusted OOS edge is not proven;
- authoritative point-in-time derivatives-context evidence remains incomplete;
- no real protected Bybit Demo ENTRY has been proven on the current operational path;
- no complete real-broker exact-head operational chain has been proven;
- no multi-week connected Demo soak has been proven;
- production KMS/HSM/workload identity, external audit and backup/restore evidence remain incomplete;
- independent live approval and controlled tiny-capital live pilot evidence are absent.

The release process remains intentionally fail-closed while any required live-critical blocker is unresolved.
