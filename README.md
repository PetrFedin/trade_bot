# ASTRA Trade Bot

ASTRA is a fail-closed trading-system platform with a mature deterministic execution/risk/OMS core and an intentionally separate strategy-research and external-broker qualification process. The repository must not be described as production-profitable until both the operational and profitability evidence gates are independently satisfied.

## Current system identities

The current identities are intentionally separated:

- **Canonical main:** `main`; last fully re-qualified engineering baseline observed on 2026-09-02 is `0bc1e309e1b268756023088b6e440db30cb4ddab`.
- **Operational boundary candidate:** PR #93 at `c2e6b11b8dc4abc37ed6b2c180f11c73a000ca5b`; code-qualified, but a real protected Bybit Demo ENTRY and complete real-broker evidence chain are **not proven**.
- **Active research head:** PR #100 at `918dbc57c0633c6dc549f1f036d2ae659b289b46`; research-only and not an operational release candidate.
- **Strategy status:** `PROFITABILITY_NOT_PROVEN`. The latest frozen Bybit price-only replay is negative; incomplete derivatives-context research does not supersede that result.
- **Live status:** `FAIL_CLOSED`; external order routing, live trading and mainnet entry remain disabled.
- **Governance:** GitHub `main` branch protection is currently `VERIFIED_DISABLED` and is a release blocker.

Machine-readable current-state details and blockers are in `CURRENT_SYSTEM_STATUS.json`. Human-readable capability evidence remains in `docs/E2E_MATRIX.md`.

## What exists now

The canonical stable core contains:

- market-data models, validation and freshness controls;
- deterministic strategy and research infrastructure with explicit no-lookahead qualification contracts;
- pre-trade risk controls for exposure, concentration, liquidity, stale prices, spread/slippage, loss, drawdown, turnover and volatility;
- immutable risk-decision evidence;
- fee-aware portfolio accounting with realized/unrealized P&L and reconciliation;
- transactional SQLite and PostgreSQL OMS/storage paths;
- deterministic client IDs, durable submit/mutation outboxes and append-only events;
- monotonic fill adoption, partial-fill handling and read-only ambiguity recovery;
- at-most-once submit/cancel/replace safety controls;
- broker-truth reconciliation and fail-closed recovery semantics;
- security/release qualification, hash-locked dependencies, SBOM/provenance and signed attestations;
- historical platform-control layers through V109, with later operational work still unmerged in the stacked PR lineage.

## Deterministic product chain

```text
market data
  -> data quality / freshness
  -> strategy target
  -> portfolio intent / sizing
  -> pre-trade risk
  -> immutable risk evidence
  -> durable OMS
  -> durable submit / mutation state
  -> broker acknowledgement / fills
  -> portfolio accounting
  -> broker-truth reconciliation
  -> safety / readiness state
```

The deterministic engineering baseline was repaired and re-qualified on canonical `main` on 2026-09-02. The covered full regression runs with PostgreSQL enabled, applies the canonical V107->V109 security migration lineage, and preserves the fail-closed live state. This does **not** substitute for real broker, Demo, soak or profitability evidence.

## External broker qualification

External broker evidence is deliberately separated from mocked/deterministic CI.

Alpaca Paper read-only workflows exist, but a workflow wrapper completing safely without credentials is not evidence of a successful broker connection. Credential-backed REST/WebSocket evidence must be present before Alpaca external integration is called qualified.

The later Bybit operational stack contains a protected one-shot Demo-entry design with fixed egress, existing ARM state, short-lived operator approval, immutable authorization/provenance, at-most-once ENTRY and mandatory post-attempt reconciliation. That path remains **Demo-unproven** until an actual protected operational execution succeeds on one exact qualified head.

## Strategy evidence

Do not infer profitability from the strength of the execution platform.

The latest frozen Bybit price-only research replay records 102 trades (36 wins, 11 breakeven, 55 losses) and approximately `-176.67 USDT` on `1,000 USDT` reference equity. Therefore the current strategy status is `PROFITABILITY_NOT_PROVEN`; no strategy promotion is allowed from that evidence.

The derivatives-context research remains incomplete until authoritative point-in-time data are acquired and frozen without outcome-driven retuning.

## Safety state

```text
external_order_routing_allowed = false
live_trading_allowed = false
mainnet_entry_allowed = false
production_release_allowed = false
```

Unknown or unreconciled broker state must fail closed. Recovery must not create a replacement ENTRY. A real-money release is a separately governed decision and cannot be enabled by a generic configuration flag.

## Current primary blockers

- server-side `main` branch protection / required-review enforcement is verified disabled — tracked in #103;
- the large stacked PR graph is not yet consolidated into canonical core, operational candidate and isolated research — tracked in #104;
- positive cost-adjusted frozen OOS edge is not proven;
- authoritative point-in-time Bybit derivatives-context qualification is incomplete;
- no protected real Bybit Demo ENTRY has been proven through the current operational path;
- no complete real-broker `INFRA -> SESSION -> SUPERVISOR -> ARM -> ENTRY -> HALT -> RECOVERY` evidence chain exists on one exact head;
- no multi-week connected Demo soak has been proven;
- production KMS/HSM, workload identity, backup/restore and external audit evidence remains incomplete;
- independent live approval and tiny-capital mainnet pilot evidence are absent.

## Source of truth

Use these artifacts together:

- `CURRENT_SYSTEM_STATUS.json` — current machine-readable identities, safety state and blockers;
- `docs/E2E_MATRIX.md` — human-readable capability evidence and readiness claims;
- `docs/RELEASE_GOVERNANCE.md` — release ownership and verified GitHub enforcement boundary;
- `docs/RELEASE_PROCESS.md` — release qualification sequence;
- `LIVE_EXECUTION_STATUS_V109.json` — current stable-main V109 production-authority flags.

No README statement overrides the E2E matrix, machine-readable status, broker evidence, profitability evidence or fail-closed live flags.
