# C2A3 — v120 persistence preservation audit

Observed: 2026-09-03

Source PR: #77

Source SHA: `6d557dd4bdb555b68789db247b9a112b962e2481`

Purpose: identify the smallest strategy-free dependency closure that can be canonicalized on top of the already-qualified C2A2 v120 schema/security boundary. This audit does **not** authorize broker, Demo, live, strategy promotion, order mutation, ARM/HALT, sizing, risk, or capital changes.

## Exact historical preservation evidence

Run `33784298848` checked the complete Git history on a GitHub-hosted runner and calculated both Git blob identity and raw-file SHA-256 directly from the source SHA.

| Historical file | Git blob | SHA-256 | Classification | C2A3 decision |
|---|---|---|---|---|
| `app/execution/bybit_demo_approval_lineage.py` | `c299f7e393f0908c7e6ced07a232a868874e4246` | `77db367e54dc7e3c1579d665e5fd2dae898ffca7ae4af509909770991bc1f7cf` | MIXED: immutable DTO/validator + OPERATIONAL_CONTROL/STRATEGY-derived builder | Extract DTO/validation semantics only; do not copy builder/imports |
| `app/execution/bybit_demo_approval_lineage_store.py` | `ae94238623bf9005f80e408e083643a9a45b4b1a` | `546e8a4823eb238a8c7ba569684c5a305ff8ac7873d36535776f8a843a781366` | PERSISTENCE + filesystem | Preserve canonical payload/hash semantics; filesystem store not required by C2A3 |
| `app/execution/bybit_demo_entry_provenance.py` | `b3b6adf27bd25f3cc982c95021dcd21e10c49900` | `0d9ef57410cd1a448425b9d7bfb5edee1930072e2d13e4b634612e277abdb16e` | MIXED: immutable DTO + STRATEGY/CYCLE/FALLBACK builder | Extract outcome-free entry-boundary record only; no builder/import ancestry |
| `app/execution/bybit_demo_entry_provenance_store.py` | `1f3b5544cd18b32f935f3558fa4698073c6f57b0` | `d8357ea8e43cc90ee574ed647ee355a49badfdae012bb509bf9414fc71440882` | PERSISTENCE + STRATEGY enum/fallback decode coupling | Preserve serialized field names and canonical hashing; replace strategy enums with persistence-neutral values |
| `app/execution/bybit_demo_profit_preservation_evidence.py` | `bcf1dc902015e8ebf8d2569a9b31aa5b6531cd1a` | `769336edd3ee1576d72adc726d5becabda5c1de518644139658a36cb28e82d6f` | MIXED: terminal DTO + ACCOUNTING/EXCURSION/STRATEGY builder | Extract terminal evidence record only; no builder/import ancestry |
| `app/execution/bybit_demo_terminal_evidence_store.py` | `f1b339430e69e2e9924a8eb84a82a7a8235ad5c4` | `11b198bc5e8b8083e7ceed5914fc0e1052fb964badc6f3cb181078f9ff0e98b9` | PERSISTENCE + terminal DTO coupling | Preserve terminal nested canonical payload/hash semantics; filesystem store not required |
| `app/execution/bybit_demo_postgres_approval_lineage_store.py` | `24639d1f88d6cab99d6745f7ef16a6d467796cf9` | `027c53112d66145adcea27ae82d96014fdfa780e7e502dbcd488a569078e3223` | POSTGRES PERSISTENCE; imports mixed DTO/store; contains bootstrap `migrate()` | Rebuild against strategy-free record contract; no runtime migration method |
| `app/execution/bybit_demo_postgres_entry_provenance_store.py` | `db1befa35ee7b079496deb6147327df45593023e` | `ba3d4d07b309bf72061c9e826bd4e66b729f0a5a30127efd3e54601b242b35d4` | POSTGRES PERSISTENCE; imports mixed DTO/store; contains bootstrap `migrate()` | Rebuild against strategy-free record contract; no runtime migration method |
| `app/execution/bybit_demo_postgres_terminal_evidence_store.py` | `015f7d652dde1e6d2eef45f2b0f53f277ffddc7d` | `8decd8548eee34c03cf780efd8b158fc3da90578366b05ff3e56d0548aa25e00` | POSTGRES PERSISTENCE; imports mixed terminal store; contains bootstrap `migrate()` | Rebuild against strategy-free terminal contract; no runtime migration method |

The temporary measurement workflow was deleted after run `33784298848`; it is not part of the intended C2A3 deliverable.

## Dependency boundary

### Approved-entry authorization

Persistence-safe facts are the historical immutable authorization fields and their fail-closed flags. The historical builder is **not** persistence-safe because it imports `bybit_demo_operator_approval`, which reaches strategy selection, market-data types, crypto strategy types, broker/order request types and session-risk state.

C2A3 therefore preserves:

- exact approval/source snapshot identities;
- evidence/market ranks;
- symbol/side;
- decision/signal/approval/expiry/authorization timestamps;
- deterministic Demo entry and close orderLinkIds;
- operator-confirmed, BYBIT_DEMO, single-use, outcome-free, diagnostics-only flags;
- `trade_actionable=false`;
- automatic retuning/promotion/mainnet flags false.

It does not import or recreate the historical operator approval builder.

### Protected-entry provenance

The historical record is explicitly outcome-free: it contains only selection/planning context plus facts available before or at the protected-entry boundary, including actual entry fill/protection economics. It intentionally excludes realized PnL, future MFE/MAE and terminal outcome.

Historical builder dependencies include strategy/cycle/ranked-fallback types. C2A3 removes those dependencies by using persistence-neutral string values for side/fallback stage and neutral immutable fallback-attempt records while preserving the serialized values and canonical field names.

C2A3 must reject hidden/extra payload keys so future/outcome fields cannot be smuggled into a nominally outcome-free canonical record.

### Terminal evidence

Terminal evidence is allowed to contain realized outcome/accounting facts because it is terminal and diagnostics-only. Historical builder dependencies include excursion tracking, post-trade accounting and crypto strategy side types.

C2A3 preserves the serialized terminal evidence facts with persistence-neutral side/outcome values, but it does not import the builders. The record must remain:

- fully reconciled all-in;
- diagnostics-only;
- unable to authorize exit-threshold retuning;
- unable to promote strategy;
- unable to permit mainnet routing.

Terminal evidence must never be used as an entry authorization or selection input by the C2A3 persistence layer.

## Canonical database boundary inherited from C2A2

C2A3 uses the already-qualified tables:

- `astra_bybit_demo_approved_entry_authorization_v120`;
- `astra_bybit_demo_entry_provenance_v120`;
- `astra_bybit_demo_terminal_evidence_v120`.

The long-running runtime role has exactly `SELECT, INSERT` on those tables. C2A3 adapters therefore expose only immutable `persist` and `load` operations. They do not expose `migrate`, UPDATE, DELETE, TRUNCATE, schema creation or any broker/network mutation.

## Serialization compatibility rule

The new persistence-safe contracts must preserve historical canonical JSON field names and Decimal string rendering so a record produced from the same logical values hashes to the same canonical SHA-256 as the historical v120 format.

Decoders are intentionally stricter than the old mixed modules in one respect: exact payload-key sets are required. This prevents unrecognized hidden outcome/action fields from surviving persistence validation.

## Explicit exclusions

C2A3 does not canonicalize:

- operator approval construction;
- strategy selector;
- ranked fallback execution;
- crypto strategy enums/configuration;
- broker client/order request types;
- Demo cycle/orchestrator;
- excursion tracker;
- post-trade accounting builders;
- market-data acquisition;
- ARM/HALT;
- order submission/cancel/protection/close;
- live/mainnet routing.

Those concerns require separate bounded gates if they are ever promoted.
