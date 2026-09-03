# ASTRA Trade Bot

ASTRA is a fail-closed trading-system platform. Its deterministic execution/risk/OMS core is materially stronger than its currently proven trading edge. The repository must not be described as production-profitable until operational, data, strategy, Demo, reliability, security, production and live-profitability gates are independently satisfied.

## Current identities — 2026-09-03

- **Canonical `main`:** `e110a4c02f5bf9b9937ff3fbf7e942859be9050d` — engineering/release baseline PASS for the covered deterministic surfaces.
- **Canonical operational foundation:** C2A0 / PR #113 — strategy-free PostgreSQL v119 Demo runtime lease extracted and qualified on `main`; no broker network or order-write capability.
- **Historical operational decomposition source:** PR #93 at `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b`; it is not a release candidate and may not be merged wholesale.
- **Active research head:** PR #100 at `918dbc57c0633c6dc549f1f036d2ae659b289b46`; research-only.
- **Strategy:** `PROFITABILITY_NOT_PROVEN`; latest frozen Bybit price-only replay remains negative.
- **Live/mainnet:** `FAIL_CLOSED`; external routing, live trading, mainnet entry and production release remain disabled.
- **Governance:** `main` branch protection is still `VERIFIED_DISABLED` and tracked by #103.

Machine-readable current status is `CURRENT_SYSTEM_STATUS.json`; human-readable evidence is `docs/E2E_MATRIX.md`.

## What is canonical now

The stable core includes:

- market-data models, validation and freshness controls;
- deterministic research infrastructure with explicit no-lookahead contracts;
- pre-trade risk, immutable risk evidence and portfolio constraints;
- fee-aware portfolio accounting and broker reconciliation;
- transactional SQLite/PostgreSQL OMS and durable mutation state;
- deterministic client IDs and at-most-once mutation safeguards;
- monotonic/replay-safe fill adoption;
- GET-first ambiguity/restart recovery;
- release security, hash-locked dependencies, SBOM and signed provenance;
- platform-control layers through V109;
- **C2A0:** the extracted v119 PostgreSQL singleton runtime lease with no TTL/stale takeover, no strategy dependency and no broker/order surface.

The v119 migration is frozen at SHA-256 `c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e`.

## Current qualification reference

Signed canonical `main` `e110a4c02f5bf9b9937ff3fbf7e942859be9050d` has current evidence including:

- canonical security regression `33731847837` — PASS; PostgreSQL-aware full suite `1078 passed / 2 dedicated fleet-deployment skips`;
- release provenance `33731847749` — PASS, including lock freshness, strict dependency audit, full regression, build/release evidence, signed SLSA provenance and signed SBOM attestation;
- external roundtrip `33731847806` — PASS;
- stable runtime import boundary `33731847773` — PASS;
- product composition `33731847986` — PASS;
- stable core quality `33731847745` — PASS;
- release governance `33731847853` — PASS while correctly recording branch protection as disabled.

These are software/release proofs, not broker or profitability proofs.

## Current operational consolidation

The historical #41–#100 stack is being decomposed rather than merged. C2A0 is the first completed canonical extraction. Old PR #110 was closed **without merge** after PR #113 reproduced the same five audited Git blobs on repaired `main`.

The next bounded gate is **C2A1 / #114**: enforce a least-privilege, non-owner PostgreSQL runtime role for canonical v119. The runtime role must be unable to `TRUNCATE`, `ALTER`, `DROP` or create schema objects while retaining only the exact DML needed by the runtime lease/checkpoint APIs.

Future v120 approval/provenance/terminal-evidence extraction remains blocked by #107 until the append-only TRUNCATE/owner-role bypass is closed. Canonical V107–V109 append-only TRUNCATE hardening remains separately tracked in #109.

## Trading research truth

The latest frozen Bybit price-only evidence remains:

- 2,394 eligible signals;
- 792 plan-eligible at 1,000 USDT reference equity;
- 617 independent target/stop episodes;
- 137 TARGET_FIRST / 471 STOP_FIRST / 9 NEITHER;
- 102 shared-capital portfolio trades;
- 36 WIN / 11 BE / 55 LOSS;
- approximately `-176.67 USDT` net P&L on `1,000 USDT` reference equity.

Therefore no positive cost-adjusted frozen OOS edge has been established. The incomplete derivatives-context experiment cannot supersede this result until authoritative point-in-time acquisition and a new frozen qualification are completed without outcome-driven retuning.

## Demo and live boundary

The later historical operational stack contains valuable protected one-shot Demo semantics — fixed egress, existing ARM, short-lived approval, immutable authorization/provenance, at-most-one ENTRY, mandatory reconciliation and reduce-only/protection recovery — but these controls are not yet fully canonicalized and no real protected Demo ENTRY is proven.

No Demo/mainnet mutation should be attempted merely because C2A0 is green. A protected Demo entry remains blocked until the required strategy-promotion and canonical operational gates are satisfied.

```text
external_order_routing_allowed = false
live_trading_allowed = false
mainnet_entry_allowed = false
production_release_allowed = false
```

## Current primary blockers

- P0: branch protection / required review enforcement disabled — #103;
- P0: positive cost-adjusted strategy edge not proven;
- P0: real protected Bybit Demo entry not proven;
- P0: exact-head real-broker INFRA -> SESSION -> SUPERVISOR -> ARM -> ENTRY -> HALT -> RECOVERY chain absent;
- P1: C2A1 v119 runtime-role least-privilege boundary — #114 / parent #107;
- P1: v120 append-only TRUNCATE/owner-role hardening — #107;
- P1: canonical V107–V109 append-only TRUNCATE hardening — #109;
- P1: authoritative point-in-time derivatives evidence incomplete;
- P1: connected multi-week Demo soak absent;
- P1: production KMS/HSM, backup/restore, external audit and related environment evidence incomplete;
- P0: independent live approval and tiny-capital mainnet pilot evidence absent.

## Source of truth

- `CURRENT_SYSTEM_STATUS.json` — current machine-readable status;
- `STACKED_PR_CONSOLIDATION_STATUS.json` — current canonicalization status;
- `docs/E2E_MATRIX.md` — current human-readable readiness matrix;
- `docs/STACKED_PR_CONSOLIDATION_MAP.md` — current extraction plan;
- `docs/archive/2026-09-02/` — preserved pre-C2A0 current-state/audit snapshots;
- `docs/RELEASE_GOVERNANCE.md` and `docs/RELEASE_PROCESS.md` — release governance/qualification;
- `LIVE_EXECUTION_STATUS_V109.json` — stable V109 production-authority flags.

Historical evidence is preserved, but no historical PASS silently qualifies a later release head.
