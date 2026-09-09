# ТЗ — критерии приёмки ASTRA / trade_bot как полноценного продукта

Дата фиксации: 2026-09-09  
Базовый SHA аудита: `ff684ab85b92151c215c7d5cc85bfc527fecb5eb`  
Статус базовой версии: **NOT_PRODUCT_READY**

## 1. Цель

Цель проекта — не наличие большого количества модулей, тестов, исторических PR или исследовательских функций, а **один канонический, воспроизводимый, безопасный и доказуемо работающий торговый продукт**.

Продукт считается готовым только тогда, когда одна exact версия кода, одна зафиксированная конфигурация и одна доказательная release identity проходят полный жизненный цикл:

```text
источник данных
-> нормализация / point-in-time identity
-> quality / freshness / clock validation
-> сигнал
-> immutable strategy release identity
-> план сделки
-> whole-portfolio valuation
-> session + portfolio risk
-> pending-risk reservation
-> exact risk authorization
-> OMS intent identity
-> durable outbox
-> exclusive external-mutation claim
-> final readiness/HALT interlock
-> единственная внешняя мутация
-> broker response validation / uncertainty
-> durable execution fact
-> partial/full fill accounting projection
-> position protection / supervision
-> close / reduce-only recovery
-> fees / funding / all-in PnL
-> broker/account reconciliation
-> terminal evidence
-> restart/recovery parity
-> готовность к следующей операции
```

Ни один этап не считается пройденным только потому, что соответствующий класс, файл, unit test или исторически зелёный PR существует.

## 2. Базовые принципы приёмки

1. **Один канонический продукт.** Historical stacked PR и research branches — источники семантики/доказательств, но не части релиза сами по себе.
2. **Одна доказательная identity.** Candidate head, synthetic PR merge, post-merge main и release artifact identities фиксируются отдельно и честно.
3. **Fail-closed.** Unknown/missing/stale/contradictory state не превращается в healthy default для нового риска.
4. **Broker execution fact выше внутренней модели.** Реальный execution нельзя удалить или игнорировать из-за ошибки accounting projection.
5. **At-most-once = право на внешний POST.** Идемпотентная запись состояния недостаточна; external mutation capability получает только один владелец.
6. **No blind mutation retry.** Ambiguous submit/amend/cancel сначала разрешается GET/read/reconcile.
7. **Risk-increasing change = new admission.** Quantity/price/notional/margin increase требует повторного risk approval.
8. **Pending risk — first-class state.** Open/pending/outbox/reserved exposure участвует в risk truth.
9. **Technical ID защищает economics.** Same ID с другим payload/economics = conflict, а не idempotent success.
10. **Readiness проверяется в последней точке перед риском.** Старый plan/readiness не авторизует будущий POST.
11. **Historical/replay и operational modes физически различаются.** Replay clock нельзя случайно использовать как live freshness clock.
12. **Demo не равно production.** Paper/Demo qualification не доказывает mainnet.
13. **Engineering readiness не равно profitability.** Без отдельного frozen positive cost-adjusted OOS evidence стратегия не продвигается.
14. **Recovery не создаёт новую позицию.** Recovery может protect/reduce/flatten/reconcile existing exposure, но не replacement ENTRY.
15. **Installed artifact должен быть самодостаточен.** Production runtime не зависит от Git checkout/current working directory для SQL/config/schema assets.

## 3. Каноническая архитектура и обязательные acceptance contracts

### 3.1 Data acquisition, качество и время

Обязательные данные/состояния:

- source event/exchange timestamp;
- received timestamp;
- decision timestamp;
- local monotonic/wall clock;
- exchange/server time where available;
- explicit mode: `REPLAY` / `OPERATIONAL`;
- freshness SLA;
- gap/duplicate/out-of-order/future detection;
- point-in-time source manifest;
- source outage/unavailable status.

Operational path обязан использовать **validated data snapshot/token**, который невозможно получить при failed quality gate.

Не допускается:

- вызывать strategy на stale/gapped/future/non-monotonic data и затем отдельно игнорировать failed validator;
- считать historical data свежими относительно их собственного последнего timestamp;
- падать uncaught exception на уже определённом invalid input вместо fail-closed result;
- подставлять future values/zeros вместо unavailable source state.

Acceptance:

- stale/gap/jump/future/duplicate/non-monotonic/mixed-symbol/invalid timestamp scenarios => no risk-increasing intent;
- malformed final bar => deterministic `ready=false`, а не naive/aware `TypeError`;
- replay остается deterministic, но явно не выдаёт historical freshness за operational readiness.

Связанные findings: F04, F14, F16 / #136.

### 3.2 Strategy/research и promotion

Research artifact обязан иметь immutable identity:

- Git SHA / strategy code hash;
- config/parameters;
- universe;
- dataset/source manifest;
- train/validation/OOS/holdout boundaries;
- cost model;
- decision/intrabar assumptions;
- qualification policy version.

`qualified=true` запрещён без non-vacuous evidence. Минимум:

- `total_trades >= policy minimum`;
- `active_oos_windows >= policy minimum`;
- достаточные regimes/time periods;
- достаточная cross-symbol/sample coverage;
- fees/spread/slippage;
- funding/borrow where applicable;
- positive frozen net OOS criterion;
- drawdown/tail criterion;
- concentration/sensitivity checks;
- point-in-time/no-lookahead proof;
- final untouched holdout policy.

`0 trades`, `0 active windows` или insufficient sample => explicit FAIL reason.

Promotion path:

```text
RESEARCH
-> QUALIFIED_RESEARCH
-> independently approved immutable strategy release
-> DEMO_CANDIDATE
-> DEMO_PROVEN
-> LIVE_CANDIDATE
```

Runtime не должен hardcode validation strategy и одновременно считать её promoted strategy.

Текущее frozen Bybit price-only evidence остаётся отрицательным; статус **`PROFITABILITY_NOT_PROVEN`** сохраняется.

Связанный finding: F15 / #139.

### 3.3 Whole-portfolio valuation

До любого нового risk admission должен существовать authoritative valuation snapshot для:

- всех held positions;
- all acknowledged/open orders;
- pending/outbox reservations;
- partially filled residuals;
- execution facts not yet projected;
- recovery-only exposure.

Каждый required symbol получает fresh valid price с timestamp/source identity.

Если хотя бы одна существующая exposure не может быть оценена — новый risk блокируется.

Связанный finding: F08.

### 3.4 Operational risk context

Для `OPERATIONAL` режима отсутствуют optimistic defaults.

Каждый enabled risk limit должен явно объявлять required observations. Примеры:

- `market_open` / session status;
- halt state;
- cash/equity;
- portfolio positions;
- reserved exposure;
- ADTV/liquidity;
- spread/slippage;
- sector/classification;
- realized/annualized volatility;
- session/daily PnL;
- drawdown/high-water;
- turnover;
- margin/liquidation data for derivatives.

Если limit включён, а required observation неизвестно, результат = explicit fail-closed reason (`*_UNKNOWN`), а не пропуск проверки.

Связанный finding: F13 / #136.

### 3.5 Pending-risk reservation

Перед созданием risk-increasing outbox должен атомарно появиться durable reservation.

Reservation содержит минимум:

- intent identity;
- symbol/side;
- quantity;
- reference/limit economics;
- reserved cash/notional/margin;
- risk revision;
- lifecycle state.

Admission использует:

```text
positions
+ open broker orders
+ local pending/outbox reservations
+ unprojected execution exposure
```

Reservation освобождается/изменяется только после доказанного fill/cancel/reject/reconciliation outcome.

Несколько заявок, каждая из которых отдельно проходит limit, но вместе превышают limit, должны блокироваться.

Связанный finding: F03.

### 3.6 Exact risk authorization

Нельзя передавать lower lifecycle просто `approved=True`.

`RiskAuthorization` должен быть immutable и содержать:

- decision id;
- exact intent id;
- canonical economics hash/revision;
- risk policy identity;
- measured context identity;
- approval timestamp/expiry where applicable;
- immutable evidence hash.

OMS/outbox не принимают approval для другого intent/economics.

Связанный finding: F06.

### 3.7 OMS intent identity и conflict-aware idempotency

`intent_id` = immutable economic identity.

Identical replay разрешён только если совпадают declared identity fields, как минимум:

- symbol;
- side;
- quantity;
- reference/limit price contract;
- strategy release identity;
- created/decision identity where part of canonical intent.

Same `intent_id` + different economics => deterministic `INTENT_ID_CONFLICT`.

То же правило применяется к event ids:

- identical event id + identical canonical payload = idempotent replay;
- identical event id + different payload/target/broker identity = conflict.

Reference implementation principle — conflict-aware strict portfolio store.

Связанный finding: F20 / #138.

### 3.8 External mutation claim

Risk-increasing submit lifecycle должен содержать отдельный exclusive claim:

```text
INTENT
-> RISK_APPROVED
-> RESERVED
-> OUTBOXED
-> EXCLUSIVE_CLAIM(owner/fence)
-> FINAL_READINESS_CHECK
-> SUBMIT_STARTED
-> external POST exactly once by claim owner
-> broker truth | UNCERTAIN
-> reconciliation
```

Требования:

- SQLite and PostgreSQL atomic semantics;
- multiprocess races;
- only winner receives mutation capability;
- loser cannot POST and goes read/reconcile only;
- crash before/after claim;
- crash before/after POST;
- crash after broker success/lost response;
- no TTL-based unsafe takeover without audited recovery.

Cancel/replace используют аналогичный winner-only contract.

Связанные findings: F01, F02.

### 3.9 Final readiness/HALT interlock

Immediately before every risk-increasing external call система обязана заново проверить authoritative current state:

- durable HALT/ARM;
- current readiness;
- market-data validity;
- broker connectivity;
- accounting convergence;
- reservation ownership;
- session risk;
- authorization validity;
- current runtime/release identity.

Этот recheck находится в той же доверенной mutation boundary, что и exclusive claim.

Старый outbox/approval не имеет права обходить новый HALT/readiness failure.

Risk-reducing protect/flatten/recovery путь остаётся отдельно разрешаемым по policy.

Связанный finding: F05 / #140.

### 3.10 Submit response identity / broker economics

Broker response до adoption сверяется с exact authorized request:

- client order id;
- broker order id shape/identity;
- symbol;
- side;
- order quantity;
- price/order economics;
- status;
- filled quantity;
- broker timestamp.

Если venue нормализует tick/price, tolerance/rounding contract должен быть explicit, frozen and tested.

Material divergence => `UNCERTAIN`/quarantine/reconciliation, не ACK success.

Связанный finding: F17 / #138.

### 3.11 Broker order replacement lineage

После первоначального ACK primary broker order identity нельзя silently overwrite.

Допустимые модели:

1. immutable primary broker id + append-only successor lineage; либо
2. explicit `replaces/replaced_by` chain.

Новый broker order id принимается только при exact successful replace mutation evidence.

Fill/backfill mapping должен уметь однозначно сопоставлять legitimate original/successor identities.

Связанный finding: F18 / #138.

### 3.12 Broker event / WebSocket trust

External execution event проходит:

- authenticated stream/listening state;
- schema validation;
- parser agreement;
- client/broker order identity;
- symbol/side/order qty checks;
- monotonic cumulative fill/time;
- validated dedup provenance;
- durable execution inbox append.

Rejected/untrusted digest не может стать trusted только потому, что он присутствует в low-level seen-set.

Valid duplicate должен иметь один economic effect.

PR #134 ремонтирует bounded F12 accounting-authority bypass, но не считается полной квалификацией всего stream subsystem.

Связанный finding: F12.

### 3.13 Immutable execution facts и accounting projection

Должны быть разделены:

1. **broker execution fact**;
2. **accounting projection state**;
3. **OMS cumulative adoption**;
4. **readiness/convergence state**.

Target flow:

```text
validated execution
-> durable immutable execution inbox
-> accounting projection PENDING
-> apply idempotent cash/position/fees/funding
-> durable projection receipt/checkpoint
-> OMS cumulative convergence
-> reconciliation
-> PROJECTED
```

При projection failure:

- execution fact сохраняется;
- status = `PENDING/QUARANTINED`;
- new risk blocked;
- restart deterministic replay resumes projection;
- adverse execution не удаляется ради прохождения internal invariant.

Связанные findings: F10 / #135, #138.

### 3.14 Immediate fills in submit response

Если submit response уже содержит `PARTIALLY_FILLED` или `FILLED`, он обязан создать тот же durable execution/accounting path, что WebSocket/GET backfill.

Недопустимо состояние:

```text
OMS = FILLED
portfolio = old position
next strategy cycle = allowed
```

До convergence следующий risk-increasing plan/dispatch заблокирован.

Связанный finding: F19 / #138.

### 3.15 Account/session genesis

Opening cash/equity не может быть arbitrary replay argument для уже существующей history.

Нужен immutable genesis:

- account/session identity;
- opening cash/equity;
- currency;
- created timestamp;
- source/evidence identity;
- ledger revision.

Изменение капитала после genesis — explicit cash-flow event, а не новый constructor argument.

Связанный finding: F11.

### 3.16 Restart/recovery

После restart exact durable state обязан восстановить:

- OMS;
- mutation claims/state;
- broker execution facts;
- accounting projection checkpoints;
- portfolio positions/cash;
- reservations;
- session risk/high-water;
- HALT/ARM state;
- active-trade checkpoint;
- terminal evidence.

Unknown/mismatch => no new exposure.

Recovery не создаёт replacement entry.

### 3.17 Bybit Demo operational plane

Canonical product должен bounded-способом объединить required semantics без wholesale merge historical stack:

- v119 runtime lease + active checkpoint;
- v120 approval/provenance/terminal evidence;
- v121 ARM/HALT;
- v122 restart-safe session risk;
- one-time session start;
- terminal evidence -> risk commit -> checkpoint ACK;
- persistent supervisor;
- v123 audited lease recovery;
- connected read-only account/position/order preflight;
- dedicated write-key metadata proof;
- fixed-egress protected zone;
- same-account / operational-zone identity;
- v124 logical DB identity/bootstrap/readiness;
- exact-symbol short-lived operator approval;
- canonical protected Demo ENTRY;
- verified protection/reduce-only recovery;
- exact release evidence chain.

До устранения P0 product acceptance gaps реальный новый Demo ENTRY не выполняется только ради проверки кода.

### 3.18 Runtime application / observability

Полноценный продукт имеет один supported long-running service/operator interface, а не только library builders.

Обязательные surfaces:

- config validation;
- version/release identity;
- DB verify/bootstrap;
- startup preflight;
- runtime status;
- health/readiness/liveness;
- graceful shutdown;
- reconciliation/recovery commands;
- structured logs with correlation ids;
- metrics;
- alerts;
- runbooks;
- deployment/service definition;
- rollback;
- backup/restore.

Authoritative `OperationalSnapshot` собирается из actual runtime state, а не hand-built optimistic values.

Минимальные metrics/alerts:

- market data age/quality;
- stream state/silence/reconnect;
- broker latency/errors/rate limits;
- queue depth / oldest outbox age;
- reservation exposure;
- uncertain/reconciling/manual age;
- execution fact -> projection lag;
- broker/local mismatch;
- positions/equity/PnL/drawdown;
- HALT/ARM/session state;
- protection state;
- DB/storage health;
- backup/restore status.

Связанный issue: #140.

### 3.19 Release package

Installed artifact содержит всё runtime-required:

- Python code;
- all SQL migrations;
- schema/config resources;
- safe templates;
- version/commit/resource digests;
- bootstrap/readiness tooling;
- release manifest/SBOM/provenance.

Runtime resources загружаются через immutable package-resource mechanism, а не CWD/repository root.

Release acceptance выполняется из clean environment:

1. install exact wheel;
2. repository source отсутствует из `PYTHONPATH`;
3. verify identity/resources;
4. bootstrap/verify empty PostgreSQL через packaged resources;
5. local/offline self-test;
6. restart parity;
7. build supported operator service;
8. prove no broker network is needed for installation qualification;
9. verify artifact resource hashes.

Связанный issue: #137.

### 3.20 Security/governance

Обязательны:

- protected `main`;
- PR required;
- required checks;
- stale approval dismissal;
- no force push/delete;
- CODEOWNERS/review ownership execution/risk/migrations;
- protected production environment;
- independent live approval;
- non-owner least-privilege runtime DB roles;
- no UPDATE/DELETE/TRUNCATE bypass append-only evidence;
- workload identity/KMS/HSM or equivalent production secret architecture;
- rotation/revocation drill;
- dependency/action pinning and audit.

Текущие blockers: #103, #109.

### 3.21 Reliability / fault campaign

Обязательные scenarios:

- multi-worker submit/cancel/replace races;
- process kill at every mutation phase;
- DB disconnect/restart;
- WebSocket disconnect/reconnect/out-of-order/duplicate;
- REST timeout/429/5xx;
- broker success + lost response;
- immediate/partial fills;
- accounting crash after execution fact append;
- projection success + OMS crash;
- cancel/replace collision;
- stale data/clock skew;
- broker identity/economics mismatch;
- missing protection;
- orphan lease;
- backup/restore/failover.

Hard acceptance:

- zero unintended duplicate ENTRY;
- zero silent exposure increase after unknown state;
- zero unresolved orphan exposure at release gate;
- every uncertain mutation converges through reads/reconciliation;
- every factual execution remains durable;
- no new risk while accounting/valuation/readiness is unknown;
- recovery never creates replacement entry.

### 3.22 Connected Demo soak

После first protected Demo chain нужен connected soak, который включает:

- no-signal periods;
- blocked signals;
- approved trades;
- partial/normal fills where naturally produced;
- reconnects;
- process restarts;
- DB reconnect/maintenance;
- HALT/recovery drill;
- backup/restore drill;
- prolonged SLO telemetry.

Исторические/local tests не заменяют connected soak evidence.

### 3.23 Live gate

Mainnet ENTRY запрещён, пока отдельно не PASS:

- full product acceptance;
- positive frozen strategy evidence;
- protected connected Demo chain;
- soak/fault recovery;
- governance;
- production security;
- DR/backup/restore;
- observability/alerts/runbooks;
- independent live approver.

Первый live шаг — отдельный tiny-capital pilot с hard notional/drawdown limits и без automatic scaling.

## 4. Обязательная E2E-матрица

| Сценарий | Ожидаемый результат |
|---|---|
| Valid live-like data | quality PASS -> signal/risk path доступен |
| Stale/gapped/future/invalid data | no risk-increasing intent |
| Enabled risk limit + missing required observation | explicit fail-closed `*_UNKNOWN` |
| Zero-trade research | strategy qualification FAIL |
| Normal order | one external submit, verified broker identity, eventual accounting convergence |
| Two workers same submit | max one external POST |
| Two workers cancel/replace | max one external mutation |
| Lost broker response | no retry POST; GET/reconcile only |
| Pending risk exceeds cap | subsequent reservation/admission blocked |
| Risk-increasing replace | new admission required |
| HALT after approval, before submit | no risk-increasing broker POST |
| Broker ACK economics differ materially | UNCERTAIN/quarantine, not success |
| Broker order id changes without replace lineage | conflict/quarantine |
| Same intent id, same economics | idempotent replay |
| Same intent id, changed economics | `INTENT_ID_CONFLICT` |
| Immediate FILLED submit response | durable execution fact + projection before next new risk |
| Unauthenticated WS fill | no accounting effect |
| Valid duplicate fill | exactly one economic effect |
| Accounting projection failure | execution fact durable, new risk blocked, restart recovery deterministic |
| Existing position in another symbol | whole-portfolio valuation succeeds with fresh prices or blocks new risk |
| Restart same history/genesis | same cash/positions/session revisions |
| Restart with different opening capital | rejected absent explicit cash-flow/genesis change |
| Clean wheel install | packaged SQL/resources/bootstrap/status/self-test available without Git checkout |
| Runtime health degraded after outbox | final interlock blocks new risk |
| PostgreSQL/process/stream fault campaign | convergence fail-closed |
| Bybit exact-head Demo chain | one exact identity proves INFRA -> SESSION -> SUPERVISOR -> ARM -> ENTRY -> HALT -> RECOVERY |

## 5. Текущие обязательные blockers и ownership

### P0

- #132 parent product acceptance;
- #134 bounded F12 repair (draft until qualification contract resolved);
- #135 F10 execution facts/accounting projection;
- #136 data/risk fail-closed gate;
- #137 self-contained installed release;
- #138 execution/OMS economic identity + immediate-fill convergence;
- #140 runtime snapshot/telemetry/final dispatch;
- F01/F02 winner-only mutation claim;
- F03 reservation ledger;
- F06 exact authorization binding;
- F07 amendment re-admission;
- #103 server-side main protection.

### P1

- F04 clock modes;
- F08 whole-portfolio valuation;
- F11 immutable genesis;
- #139 strategy qualification/promotion;
- #109 append-only physical hardening;
- exact-head/synthetic-merge evidence identity clarity;
- remaining bounded Bybit C2B/C2C/C1/C3 canonicalization;
- connected soak;
- production DR/security/observability evidence.

### P2

- F09 positive target quantity config validation;
- stale/duplicate stacked PR retirement only after #104 preservation decisions.

## 6. Definition of Done

`PRODUCT_READY = PASS` разрешён только если одновременно:

1. F01-F20 закрыты согласно required safe behavior либо formally superseded более сильным доказанным контрактом.
2. Все P0 acceptance tests PASS на SQLite/PostgreSQL, а race/crash cases — multiprocess/fault tests.
3. Data quality и operational risk context являются обязательным fail-closed path.
4. Pending reservations входят в portfolio/risk truth.
5. Exact risk authorization cryptographically/immutably связана с intent economics.
6. External mutation rights exclusive и crash-safe.
7. Broker response/identity lineage conflict-aware.
8. Execution facts immutable; accounting projection restart-safe; immediate/stream/backfill fills сходятся в одну truth.
9. Whole-portfolio valuation и account genesis deterministic.
10. Strategy promotion non-vacuous, frozen and evidence-bound.
11. Canonical Bybit runtime собран в `main`, а не только historical PR.
12. One supported product service/operator surface существует.
13. Installed wheel/container self-contained и clean-install qualified.
14. Authoritative runtime readiness/HALT привязаны к final dispatch.
15. Structured telemetry/alerts/runbooks/backup/restore qualified.
16. One exact release identity проходит full deterministic/PostgreSQL/security/release acceptance.
17. Real protected connected Demo chain PASS.
18. Connected soak PASS.
19. Positive frozen cost-adjusted OOS strategy evidence PASS отдельно от engineering CI.
20. Server-side governance/security/DR PASS.
21. Live остаётся отдельным independently approved gate.

До выполнения этих условий корректное утверждение о проекте:

> **Сильная инженерная торговая платформа с существенными квалифицированными компонентами; полноценный production trading product ещё не доказан. Live/mainnet правильно остаётся FAIL_CLOSED.**
