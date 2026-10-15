# ТЗ — приёмка ASTRA / trade_bot как полноценного production trading product

Дата: 2026-09-09  
Базовый exact SHA аудита: `ff684ab85b92151c215c7d5cc85bfc527fecb5eb`  
Статус базовой версии: **NOT_PRODUCT_READY**

## 1. Назначение

ТЗ фиксирует не список функций, а **обязательную сквозную архитектуру и Definition of Done** для продукта, который должен автономно и безопасно пройти путь от рыночных данных до сверенного финансового результата и восстановления после сбоя.

Целевая цепочка:

```text
provider/exchange data
-> point-in-time / operational identity
-> normalization + quality + clock validation
-> finalized market snapshot/bar
-> immutable strategy release
-> signal/target
-> fresh InstrumentSpec + executable normalization
-> whole-portfolio valuation
-> operational/session risk
-> pending reservation
-> immutable RiskAuthorization
-> OMS intent
-> durable outbox
-> exclusive mutation claim
-> final account/readiness/HALT/spec recheck
-> one external mutation
-> broker response validation / UNCERTAIN
-> immutable execution fact
-> financial activity recovery
-> accounting projection
-> position protection/supervision
-> close/reduce-only recovery
-> broker/account reconciliation
-> terminal evidence
-> restart/recovery parity
-> next decision allowed
```

Любой отсутствующий или неизвестный обязательный state для **нового риска** означает fail-closed.

## 2. Принципы

1. **Один canonical product.** Historical/research branches не являются частями production runtime автоматически.
2. **One exact identity.** Code SHA, config, strategy, data, instrument specs, account/credential generation и release artifact должны иметь доказуемые identities.
3. **Broker truth не удаляется внутренней ошибкой.** Execution/financial activity сначала сохраняется как факт, потом проектируется.
4. **At-most-once относится к внешнему mutation right.** Идемпотентная запись сама по себе не предотвращает два POST.
5. **Pending risk входит в exposure.** Outbox/open/partial/unprojected состояние резервирует risk capacity.
6. **Risk оценивает executable economics.** Сначала tick/lot/min-notional normalization, затем admission.
7. **Unknown operational state = reject.** Никаких optimistic zero/default для включённого risk control.
8. **No blind mutation retry.** Неопределённая мутация разрешается чтением/reconciliation.
9. **Readiness является final interlock.** Старый approval/outbox не переживает новый HALT/readiness/account/spec mismatch.
10. **Recovery не создаёт replacement ENTRY.** Только protect/reduce/flatten/reconcile existing exposure.
11. **Research, Demo, production и profitability — независимые qualification domains.**
12. **Installed artifact самодостаточен.** Runtime-required SQL/config/schema не зависят от Git checkout/CWD.
13. **DR доказан восстановлением, а не декларацией.**

## 3. Data architecture

### 3.1 Historical / research data

Qualification dataset обязан фиксировать:

- provider/feed;
- requested symbols и point-in-time entity mapping;
- `asof`/effective identity date где API поддерживает;
- universe membership на момент исследования;
- raw/adjusted corporate-action policy;
- time zone/session/calendar;
- request parameters/page identities;
- acquisition timestamp;
- immutable manifest/hash;
- train/validation/OOS/holdout boundaries.

Для corporate actions разрешены только coherent modes:

**A. RAW + explicit actions** — raw bars + split/dividend/etc. events применяются к cash/positions ровно один раз.

**B. ADJUSTED analytical series** — adjustment policy frozen; та же action не применяется повторно как cash/share event.

Re-fetch frozen dataset на другую дату либо reproduces exact identity, либо создаёт explicit data-drift failure/new dataset identity.

Связанный blocker: F27 / #147.

### 3.2 Operational realtime data

Продукт обязан сам владеть runtime path:

```text
source event
-> normalized event
-> finalized bar/snapshot
-> continuity/freshness
-> durable watermark
-> exactly-one decision
```

Обязательные поля:

- provider/venue/environment;
- instrument identity;
- source/exchange timestamp;
- receive timestamp;
- interval/bar boundaries;
- final/closed flag;
- OHLCV/quote values согласно strategy contract;
- source sequence/message id где доступно;
- canonical hash/revision.

Runtime должен иметь:

- subscription acknowledgement;
- heartbeat/silence detection;
- duplicate/out-of-order handling;
- reconnect;
- GET-only gap repair;
- market/session/calendar handling;
- durable last-finalized/last-decided watermark;
- multi-symbol completeness rule;
- one-decision-per-final-bar guarantee.

In-progress/stale/future/gapped data не могут создавать new-risk intent.

Связанные blockers: F04/F14/F16/F22 / #136/#142.

## 4. Strategy and promotion

### 4.1 Immutable strategy release

Runtime candidate содержит:

- exact code SHA/hash;
- strategy/config parameters;
- universe;
- dataset manifest;
- cost model;
- research policy version;
- qualification metrics;
- independent approval identity.

### 4.2 Non-vacuous qualification

`qualified=true` запрещён при:

- zero trades;
- zero active OOS windows;
- insufficient regimes/sample;
- missing required fee/spread/slippage/funding/borrow model;
- missing point-in-time proof;
- reused/non-independent holdout;
- unacceptable concentration/sensitivity/tail risk.

Promotion states:

```text
RESEARCH
-> QUALIFIED_RESEARCH
-> DEMO_CANDIDATE
-> DEMO_PROVEN
-> LIVE_CANDIDATE
```

Engineering CI не меняет strategy promotion state.

Текущий статус остаётся `PROFITABILITY_NOT_PROVEN`.

Связанный blocker: F15 / #139.

## 5. InstrumentSpec and executable economics

Перед каждым ordinary ENTRY/amend требуется fresh immutable `InstrumentSpec`:

- venue/environment/category;
- symbol/instrument id;
- trading status;
- base/quote/settle identity;
- min/max price;
- tick size;
- min/max quantity;
- quantity step;
- minimum notional;
- leverage min/max/step where applicable;
- margin/position-mode support;
- observed/source timestamp;
- canonical revision/hash.

Desired order нормализуется детерминированно. Rounding не имеет права увеличивать риск незаметно.

RiskAuthorization связывается именно с **normalized executable economics + InstrumentSpec revision**.

Spec stale/revision change перед dispatch => block/re-admit.

Для derivatives reduce-only/position mode/position side/size проверяются отдельно так, чтобы recovery не мог открыть opposite exposure.

Связанный blocker: F23 / #143.

## 6. Portfolio/account financial truth

### 6.1 Immutable genesis

Opening cash/equity фиксируются immutable session/account genesis:

- account/session identity;
- currency;
- opening cash/equity;
- source evidence;
- timestamp;
- revision.

Изменения капитала после genesis — explicit cash-flow activities, а не новый constructor argument.

### 6.2 Whole-portfolio valuation

До нового risk admission нужны fresh prices для:

- held positions;
- open broker orders;
- pending reservations;
- partial residuals;
- unprojected executions;
- recovery-only exposure.

Неоценимая exposure => new risk blocked.

### 6.3 Broker financial activities

Durable financial activity inbox содержит:

- activity id/type/subtype;
- account identity;
- cash amount;
- symbol/qty where applicable;
- booked timestamp;
- source cursor/page identity;
- canonical payload hash;
- projection status.

Поддерживаются все financially relevant activity types выбранного broker scope: executions/fees, deposits/withdrawals, dividends/adjustments/withholding, interest и иные признанные cash-impacting events.

Unknown cash-impacting activity => quarantine/HALT new risk.

Backfill cursor restart-safe и допускает overlap только при conflict-aware event identity.

Known `CASH_MISMATCH` блокирует new risk.

Связанные blockers: F08/F11/F21 / #141.

## 7. Operational risk

Для каждого enabled limit формально объявляются required measured inputs.

Типовые inputs:

- market open/session status;
- halt state;
- cash/equity;
- positions;
- pending/reserved exposure;
- liquidity/ADTV;
- spread/slippage;
- volatility;
- sector/classification;
- PnL/high-water/drawdown;
- turnover;
- margin/liquidation context.

В `OPERATIONAL` mode missing required observation => stable explicit `*_UNKNOWN` rejection.

Никакие implicit `market_open=True`, `halted=False`, zero spread, zero slippage, zero loss/drawdown не разрешены как незаметные defaults.

Связанный blocker: F13 / #136.

## 8. Reservations and RiskAuthorization

### 8.1 Reservation

Перед risk-increasing outbox атомарно создаётся durable reservation:

- intent identity;
- instrument/spec revision;
- side/qty/price economics;
- reserved cash/notional/margin;
- risk revision;
- lifecycle state.

Admission считает:

```text
positions
+ open broker orders
+ pending/outbox reservations
+ unprojected executions
+ recovery-only exposure
```

Reservation release/update допускается только после доказанного broker truth.

### 8.2 Exact authorization

`RiskAuthorization` содержит:

- decision id;
- exact intent/economics hash;
- policy revision;
- measured context revision;
- InstrumentSpec revision;
- account/session identity;
- timestamp/expiry;
- immutable evidence hash.

Lower execution layer не принимает голый `approved=True`.

Связанные blockers: F03/F06/F07.

## 9. OMS, idempotency and mutation rights

### 9.1 Conflict-aware identity

Same technical id:

- identical canonical payload => idempotent replay;
- different economics/payload/target/broker identity => conflict.

`intent_id` является immutable economic identity. Divergent replay => `INTENT_ID_CONFLICT`.

### 9.2 Exclusive external mutation claim

Submit/amend/cancel lifecycle:

```text
READY
-> EXCLUSIVE_CLAIM(owner/fence)
-> FINAL_INTERLOCK
-> MUTATION_STARTED
-> exactly one external call by winner
-> broker truth | UNCERTAIN
-> GET/reconcile
```

Loser никогда не получает POST capability.

Тестируются:

- multiprocess race;
- crash before/after claim;
- crash before/after network call;
- broker success + lost response;
- stale owner/recovery;
- no blind mutation retry.

Связанные blockers: F01/F02/F20 / #138.

## 10. Broker/account/environment identity

Canonical `BrokerIdentity` связывает:

- broker/venue;
- environment;
- expected account/UID privacy-safe identity;
- account type/currency/settle domain;
- credential purpose;
- credential fingerprint/generation;
- endpoint identity;
- release SHA;
- verified timestamp/expiry.

Read-only startup preflight проверяет account/environment/permissions/credential generation.

Session/ARM/approval ссылаются на exact BrokerIdentity revision.

Credential/account/release change инвалидирует old new-risk authority.

Final dispatch повторно проверяет same account/environment/credential generation.

Raw secrets не сохраняются.

Concrete F26 bounded repair PR #146 добавляет account fingerprint в external Paper readonly evidence и mismatch block до mutation, но полный F25 остаётся открытым.

Связанный blocker: F25/F26 / #145/#146.

## 11. Broker response and order lineage

Submit response сверяется с exact authorization:

- client id;
- broker order id;
- symbol;
- side;
- quantity;
- price/economics;
- status;
- filled quantity;
- broker timestamp.

Material mismatch => `UNCERTAIN`/quarantine, а не ACK success.

Legitimate venue rounding допускается только согласно frozen normalization contract.

Первичный broker order identity immutable либо имеет explicit `replaces/replaced_by` chain, доказанную exact mutation evidence.

Связанные blockers: F17/F18 / #138.

## 12. Execution facts and accounting convergence

Все execution channels — submit response, WebSocket, GET/backfill — сходятся в один immutable execution inbox.

Flow:

```text
validated execution
-> durable execution fact
-> projection PENDING
-> cash/position/fee/funding projection
-> durable projection receipt
-> OMS cumulative convergence
-> broker/account reconciliation
-> PROJECTED
```

Projection failure:

- факт сохраняется;
- new risk blocked;
- restart replay deterministic;
- никакого удаления execution ради внутреннего invariant.

Immediate `PARTIALLY_FILLED/FILLED` submit response не может оставить OMS ahead of portfolio и позволить следующий ENTRY.

Связанные blockers: F10/F19/F21 / #135/#138/#141.

## 13. Broker-event trust

External event accounting authority требует:

- authenticated stream/listening state;
- schema validation;
- parser agreement;
- account/order identity;
- monotonic/correction-aware broker contract;
- validated provenance;
- durable execution/activity fact.

Rejected/untrusted digest не может стать trusted из-за presence in low-level dedup set.

PR #134 — bounded F12 repair; он не доказывает весь broker-event subsystem.

## 14. Authoritative runtime readiness and final dispatch

Runtime `OperationalSnapshot` собирается только из actual state:

- realtime data freshness/continuity;
- feed/stream status;
- fresh InstrumentSpec;
- broker/account identity/status;
- OMS uncertain/reconciling states;
- reservations;
- execution/accounting lag;
- financial activity watermark;
- whole-portfolio valuation;
- session risk;
- HALT/ARM;
- DB/storage health;
- release identity;
- backup freshness.

Final interlock непосредственно перед risk-increasing external mutation требует clean current snapshot.

Risk-reducing recovery имеет отдельный ограниченный policy.

Связанный blocker: F05/F25 / #140.

## 15. Product service and observability

Полноценный продукт имеет официальный long-running runtime/operator surface:

- version/release identity;
- config validation;
- DB verify/bootstrap;
- startup preflight;
- live-data service;
- worker/scheduler;
- health/readiness/liveness;
- reconciliation/recovery commands;
- graceful shutdown;
- structured logs/correlation ids;
- metrics/alerts;
- deployment definition;
- rollback/runbooks;
- backup/restore status.

Минимальные alerts:

- stale feed/gap;
- stream disconnect;
- broker error/rate-limit storm;
- pending queue age;
- reservation anomaly;
- execution/accounting lag;
- broker/local mismatch;
- cash/equity mismatch;
- unknown activity/instrument spec;
- HALT/session loss/drawdown;
- orphan exposure/lease;
- backup/restore failure.

Связанный blocker: #140.

## 16. Installed release

Wheel/container должны содержать:

- Python runtime;
- all required SQL migrations;
- schema/config resources;
- safe templates;
- version/SHA/resource digests;
- bootstrap/readiness tooling;
- release manifest/SBOM/provenance.

Runtime resource loading не зависит от CWD/Git root.

Clean-install qualification:

1. install exact wheel;
2. remove source checkout from `PYTHONPATH`;
3. verify resource digests;
4. bootstrap/verify isolated PostgreSQL from packaged assets;
5. build local/offline product;
6. restart parity;
7. start supported operator/service surface;
8. no broker network needed for install qualification.

Связанный blocker: #137.

## 17. Real disaster recovery

V106 evidence schema должна наполняться **реальным** backup/restore drill:

```text
representative durable state
-> actual encrypted backup
-> isolated restore
-> schema/resource/release verification
-> event/table cryptographic comparison
-> application replay
-> restored HALTED/RECONCILIATION_REQUIRED
```

Backup set включает OMS, mutations, risk, portfolio/genesis, executions, activities, reservations, watermarks, v119-v123 control/session state и DB/release identity.

Измеряются реальные RPO/RTO.

Связанный blocker: F24 / #144.

## 18. Canonical Bybit operational product

До protected Demo ENTRY в `main` должны быть bounded-способом собраны:

- v119 runtime lease/checkpoint;
- v120 approval/provenance/terminal evidence;
- v121 ARM/HALT;
- v122 session risk;
- one-time session start;
- terminal evidence -> risk commit -> checkpoint ACK;
- active-trade supervisor;
- v123 lease recovery;
- connected read-only account/position/order preflight;
- trading-key metadata proof;
- fixed-egress trust zone;
- v124 DB identity/bootstrap/readiness;
- exact-symbol operator approval;
- InstrumentSpec normalization;
- account/credential identity binding;
- protected ENTRY;
- protection/reduce-only recovery.

Historical stacked PR не заменяет canonical product composition.

## 19. Reliability / fault campaign

Обязательные scenarios:

- submit/cancel/replace races;
- process kill at each mutation stage;
- DB disconnect/restart;
- feed/WebSocket reconnect/out-of-order/duplicate;
- REST timeout/429/5xx;
- broker success + lost response;
- immediate/partial fills;
- projection crash after execution append;
- unknown financial activity;
- broker cash mismatch;
- stale/change InstrumentSpec;
- wrong account/credential generation;
- cancel/replace collision;
- missing protection;
- orphan lease;
- backup/restore/failover.

Hard outcomes:

- zero unintended duplicate ENTRY;
- zero silent risk increase under unknown state;
- zero lost factual execution/activity;
- zero unresolved orphan exposure at release gate;
- uncertain mutations converge through reads/reconciliation;
- recovery never creates replacement entry.

## 20. Connected Demo and soak

После deterministic/PostgreSQL product PASS требуется one-exact-SHA connected evidence:

```text
INFRA_READY
-> ACCOUNT/IDENTITY_READY
-> DATA_READY
-> INSTRUMENT_READY
-> SESSION_READY
-> SUPERVISOR_READY
-> ARM_PROVEN
-> DEMO_ENTRY_PROVEN
-> ACCOUNTING_RECONCILED
-> HALT_PROVEN
-> RECOVERY_DRILL_PROVEN
```

Затем connected soak покрывает market/stream reconnects, process restarts, DB reconnect, partial fills, ambiguous mutations, reconciliation mismatch, protection, HALT/recovery и backup/restore.

## 21. Security/governance

До production:

- protected `main`;
- PR/review/required checks;
- stale approval dismissal;
- no force push/delete;
- CODEOWNERS execution/risk/migrations;
- protected production environment;
- independent live approver;
- least-privilege non-owner DB roles;
- append-only physical hardening;
- workload identity/KMS/HSM or equivalent;
- credential rotation/revocation drill;
- dependency/action provenance.

Current blockers: #103, #109.

## 22. Обязательная E2E matrix

| Сценарий | Required result |
|---|---|
| Frozen historical request | explicit point-in-time identity; reproducible dataset or drift failure |
| In-progress bar | no decision |
| Stale/gapped/future bar | no new-risk intent |
| Duplicate finalized bar | max one decision |
| Feed reconnect with gap | GET repair then resume or HALT |
| Enabled risk limit + missing input | explicit rejection |
| Existing exposure without fresh valuation | new risk blocked |
| Pending reservations exceed cap | subsequent admission blocked |
| Off-tick/off-step order | normalized safely before risk or rejected |
| Stale instrument spec | no new risk |
| Same intent id + changed economics | conflict |
| Two workers same submit | max one POST |
| Risk-increasing replace | new admission required |
| HALT after approval | no risk-increasing POST |
| Wrong account/credential generation | no POST |
| Broker ACK economic mismatch | UNCERTAIN/quarantine |
| Immediate FILLED submit | execution fact + projection before next risk |
| Unauthenticated WS fill | no accounting effect |
| Projection crash | fact durable, restart recovery, new risk blocked |
| Broker CASH_MISMATCH | no new risk |
| Unknown cash-impacting activity | quarantine/HALT |
| Restart | same durable economic/session state |
| Clean wheel install | packaged SQL/resources/service available without checkout |
| Backup/restore | exact semantic state restored; app starts HALTED |
| Connected Demo chain | one exact identity proves complete chain |

## 23. Backlog ownership

### P0

- #132 — parent product acceptance;
- #134 — F12 bounded repair;
- #135 — execution facts/projection;
- #136 — data/risk fail-closed;
- #137 — self-contained release;
- #138 — execution/OMS economic identity;
- #140 — runtime snapshot/final dispatch/telemetry;
- #141 — broker financial activities/cash truth;
- #142 — live market data/scheduler;
- #143 — InstrumentSpec normalization;
- #144 — real backup/restore;
- #145 — broker/account/credential identity;
- #146 — bounded F26 repair;
- F01/F02/F03/F06/F07;
- #103 — branch protection.

### P1

- F04/F08/F11;
- #139 — strategy qualification/promotion;
- #147 — point-in-time research;
- #109 — append-only physical hardening;
- remaining C2B/C2C/C1/C3 canonicalization;
- connected soak;
- production security/DR evidence.

### P2

- F09;
- release/User-Agent identity drift;
- stale stacked PR retirement after preservation decisions.

## 24. Definition of Done

`PRODUCT_READY = PASS` только если одновременно:

1. F01-F27 закрыты либо superseded доказанным stronger contract.
2. P0 race/crash scenarios PASS на PostgreSQL/multiprocess fault campaign.
3. Historical and operational data identities complete.
4. Live data/finalization/scheduler canonical.
5. Fresh InstrumentSpec precedes risk.
6. Operational risk missing inputs fail closed.
7. Reservations входят в exposure.
8. Exact RiskAuthorization bound to executable economics/account/spec.
9. External mutation right exclusive.
10. Broker response/order identity conflict-aware.
11. Execution and account financial activities immutable.
12. Accounting restart-safe and convergent.
13. Broker cash/equity/reconciliation authoritative for new risk.
14. Broker account/environment/credential generation bound to session/final dispatch.
15. Runtime readiness/HALT final interlock mandatory.
16. Installed release self-contained.
17. Product service/telemetry/alerts/runbooks complete.
18. Real backup/restore drill PASS.
19. Canonical Bybit runtime assembled in `main`.
20. One exact release passes full deterministic/PostgreSQL/security/fault product gate.
21. Protected connected Demo chain PASS.
22. Connected soak PASS.
23. Separate positive frozen cost-adjusted OOS strategy evidence PASS.
24. Server-side governance/security/DR PASS.
25. Independent live approval remains separate gate.

До этого состояние live/mainnet остаётся **FAIL_CLOSED**.
