# Release Process

## Principles

1. `main` is the product integration branch. Historical schema branches are implementation history, not separate products.
2. No capability or release is qualified from code or documentation alone. The **exact promoted head** must pass executable gates and have credible environment evidence.
3. Research, demo/paper qualification, tiny-capital pilot and production are separate trust domains. Promotion between them is always an explicit release decision.
4. Broker mutation ambiguity is resolved through broker-truth reads/reconciliation. Blind mutation retries are forbidden.
5. Unknown order state, failed reconciliation, unexplained cash/position delta, unsafe capability or missing protection fails closed.
6. Research results never auto-retune or auto-promote the production strategy.
7. Live/mainnet routing is never enabled as a side effect of an environment variable or ordinary deployment.

## Current integration boundary

The active Bybit integration work is on `agent/trading-quality-profit-protection` / PR #41. PR #41 is a large integration/incubator snapshot and **must not be treated as the final production merge unit**. Its purpose is to consolidate and qualify the existing trading mechanisms while keeping live/mainnet routing disabled.

Before production release work, the integration snapshot must be converted into focused, reviewable release changes from `main` with exact ownership, bounded rollback scope and release-critical CI.

## Required release sequence

1. Start from the current protected product baseline on `main`.
2. Create a focused release/integration branch; do not use an oversized incubator PR as a production release unit.
3. Keep runtime changes, migrations, configuration contracts and tests in the same reviewable change set.
4. Run fast focused gates, then the complete release-critical regression for the exact head.
5. Require safety-critical ownership review for risk, OMS, portfolio, execution, strategy, market-data and workflow changes.
6. Record exact commit SHA, strategy-policy hashes, database migration set and E2E matrix state.
7. Merge only when mandatory status checks and required reviews are enforced server-side and green.
8. Produce a version-matched signed tag and build artifacts from that tag only.
9. Generate/verify release manifest, lock evidence, security results, SPDX SBOM and signed provenance/attestations.
10. Promote separately through qualification -> controlled pilot -> production. Every promotion may fail closed independently.

## Mandatory deterministic release gates

At minimum the exact head must pass:

- complete pytest regression;
- trading-core E2E;
- canonical security regression;
- risk/market-data qualification;
- durable PostgreSQL OMS, portfolio and risk-store qualification;
- Bybit trading V2 integration;
- Bybit managed-trade / terminal-handoff / attribution lifecycle;
- liquidation/protection reconciliation;
- account closed-PnL + funding + all-in accounting reconciliation;
- strategy policy-drift / no-lookahead contracts;
- compile/lint/dependency-lock/security checks;
- CI action supply-chain policy;
- release manifest/SBOM/provenance gates where applicable.

A narrow Bybit green workflow never overrides a red global regression, and a green global regression never overrides a failed broker/environment evidence gate.

## CI tiers

The long-term release structure is intentionally tiered rather than adding more independent workflows for every new class or schema.

### PR fast gate

Target: fast feedback for affected code.

- Ruff / formatting / typing where configured;
- focused unit tests;
- affected integration contracts;
- basic security and configuration validation.

### PR full gate

- complete deterministic regression;
- PostgreSQL durability/concurrency tests;
- Bybit lifecycle/accounting/protection integration;
- stable-core E2E/security gates.

### Scheduled evidence

- historical acquisition/replay where authoritative data is available;
- 14d/28d and later multi-cycle walk-forward evidence;
- prospective directional experiment;
- long-running fault/restart/reconciliation campaigns;
- external broker qualification and soak evidence.

### Release gate

- exact release-head full regression;
- security/lock/SBOM/provenance verification;
- exact strategy-policy hashes;
- database migration manifest;
- server-side branch/review governance verification;
- immutable version-matched release tag.

## Mandatory broker/environment qualification before real capital

For the Bybit product path:

- startup broker/account/position/order reconciliation succeeds from a clean process and after forced restart;
- private execution/order/position stream and REST broker-truth reconciliation are both qualified;
- disconnect, reconnect, stream-gap, clock-drift, timeout, 429, 5xx, DNS/network-partition and lost-ACK scenarios fail safely;
- no duplicate mutation occurs through restart/recovery;
- account equity, positions, orders, closed PnL, fees and funding reconcile with the internal ledger;
- operator pause/read-only/kill controls are proven;
- production secrets are isolated from source/runtime files;
- a sustained qualification soak has no unexplained account or position deltas.

## Strategy evidence gate

Production execution correctness cannot compensate for an unproven trading edge. Before real capital, the promoted strategy must have independent forward evidence with:

- substantially larger trade sample than the current short research windows;
- multiple market/volatility regimes;
- prospective LONG/SHORT attribution without post-hoc disabling of a side;
- full actual-cost modeling including fees, funding and execution slippage;
- latency/slippage stress;
- liquidity and cost stress;
- multi-cycle walk-forward evidence;
- confidence/uncertainty reporting;
- predeclared strategy kill criteria.

The current LONG/SHORT contract remains unchanged until prospective evidence authorizes a policy change.

## Live / mainnet gate

Live/mainnet routing remains disabled until all of the following are true:

- no P0/P1 correctness or reconciliation defect exists on the exact release head;
- production state is authoritative and durable in PostgreSQL with fencing/recovery proven;
- 24/7 runtime, observability, alerts and operator controls are qualified;
- server-side `main` protection and required review/status checks are enabled and independently verified;
- production secret boundary is complete;
- strategy evidence passes the predeclared acceptance gates;
- a dedicated trading subaccount is configured with withdrawals disabled and least privilege;
- a tiny-capital pilot is independently approved as a separate release decision.

## Current release debt / blockers

Implemented and no longer generic TODOs: hash-locked dependencies, lock freshness checks, security scanning/auditing, pinned GitHub Actions policy, release manifest, SPDX SBOM, signed provenance/attestation and CODEOWNERS ownership metadata.

Still blocking a production release:

- GitHub `main` server-side branch protection / required-review enforcement is currently disabled;
- PR #41 still requires consolidation into focused release units;
- exact current integration head must remain fully green after every change;
- no version-matched signed production release tag has completed the final promotion path;
- authoritative Bybit production persistence/runtime/reconciliation/operations are not yet fully consolidated;
- independent forward strategy evidence and controlled tiny-capital approval are incomplete.

Those are executable release blockers, not documentation-only TODOs.
