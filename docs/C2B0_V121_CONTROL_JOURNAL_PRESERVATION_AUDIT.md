# C2B0 — v121 control-journal preservation audit

Observed: 2026-09-06

Canonical implementation base: `d28434f70fa5a5191c95c3e375f60833305eeee8`.
Historical source: PR #80 exact head `f40d9dee0baadd254a6fe7425b117be709088f9d`.
Tracking issue: #125.

## Runner-side byte evidence

GitHub Actions preservation run `34040180659`, job `101505386240`, checked out the exact historical source SHA and measured these artifacts:

| Historical path | Git blob | SHA-256 | C2B0 disposition |
|---|---|---|---|
| `migrations/v121/001_bybit_demo_control_plane.sql` | `cae1dd432050f235b94d230b2e46c862d38b58c6` | `a03a738d7036c59338ef2ebe085a28a266fd0b440d97ca8e3fae59379efe8e21` | preserve byte-for-byte |
| `app/execution/bybit_demo_control_plane.py` | `19c5dc7f4a1f6548b967b6b3ec43ef27d6868135` | `516cd03e1d7f72261d91c6da65484b94e27d1e66dd9cbae3b6fa8d04e15c1a59` | semantic source only; do not copy wholesale |
| `tests/test_bybit_demo_control_plane.py` | `1daa2806d154d0b1fdfa700aca75a6700bf72470` | `91e6ca5ef5b381024ccd848b1fef50bdf701441ed191540cc7f04f4fa99faedf` | semantic source only; connected/order fixtures excluded |
| `tests/test_bybit_demo_control_plane_postgres.py` | `ea78963391267a2194219e7d658535e2292b3d7f` | `25e5455c9db61b9a47e343ade4c9bb9c337c2b8323e8401150f42cb649fdeee1` | persistence semantics source only |

The temporary preservation workflow is not a product artifact and must not remain in the final PR diff.

## Preserved historical semantics

The safe persistence semantics are:

- append-only event kinds `ARM_NEW_ENTRIES` and `HALT_NEW_ENTRIES`;
- compact sorted ASCII canonical JSON: `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=True`;
- SHA-256 event identity over the historical event identity payload;
- ARM TTL strictly within `(0, 300]` seconds;
- preflight evidence age at most 30 seconds;
- future observation clock skew at most 5 seconds before clamping to the control-event timestamp;
- operator id non-empty and at most 128 characters;
- reason non-empty and at most 1000 characters;
- no-event, malformed, expired or unsafe state resolves fail-closed to HALT;
- safety markers remain immutable / no order submission / no live-mainnet routing.

## Historical code that is intentionally excluded

The historical Python module imports:

- `BybitDemoOrderRequest`;
- connected Bybit preflight result/status types;
- and implements `ControlPlaneGuardedBybitDemoClient.place_market_order`.

Those dependencies are outside C2B0. The canonical replacement must not import broker, market-data, order-request, strategy or session-risk modules and must not expose any exchange-mutation method.

The historical PostgreSQL test also couples persistence to connected-preflight construction and the v119 runtime lease. C2B0 keeps only journal persistence, rehydration, append-only and least-privilege database semantics. Operational composition belongs to later gates.

## Physical append-only gap and forward repair

Historical `v121/001` protects row `UPDATE` and `DELETE` through `astra_bybit_demo_control_append_only_v121`, but it has no statement-level `BEFORE TRUNCATE` trigger. Rewriting the historical migration would destroy preservation evidence.

Therefore C2B0 preserves `v121/001` byte-for-byte and adds `v121/002` as a forward-only hardening migration. `v121/002` must:

1. require the historical table and mutation-rejection function;
2. install an enabled statement-level `BEFORE TRUNCATE` trigger;
3. retain PUBLIC privilege revocation;
4. remain idempotent;
5. make owner-level physical TRUNCATE fail through the append-only trigger contract.

## C2B0 capability boundary

C2B0 may provide only:

1. neutral immutable v121 event records and deterministic codec/hash validation;
2. read-only latest-event / fail-closed decision rehydration;
3. append-only PostgreSQL journal writer;
4. bootstrap-only least-privilege role reconciliation;
5. separate reader and writer credentials.

It must not provide:

- connected Bybit preflight acquisition;
- broker REST/WebSocket access;
- `place_market_order`, amend, cancel, protection or close operations;
- strategy/ranking/sizing/risk economics;
- v122 session-risk state;
- automatic ARM;
- Demo ENTRY or mainnet routing;
- long-running runtime/operator migration authority.

C2B0 qualification is infrastructure/persistence evidence only and cannot promote strategy or trading readiness.
