# Полный аудит продукта ASTRA / trade_bot

Дата: 2026-09-09  
Канонический объект аудита: `PetrFedin/trade_bot`  
Exact `main`: `ff684ab85b92151c215c7d5cc85bfc527fecb5eb`  
Release: `7.39.0`  
Итоговый статус: **NOT_PRODUCT_READY**

## 1. Итоговый вывод

ASTRA уже является сильной инженерной торговой платформой, но **ещё не доказана как единый production trading product**.

Причина не в отсутствии тестов или количестве незавершённых файлов. Наоборот, компонентная база сильная: durable OMS, SQLite/PostgreSQL persistence, risk evidence, reconciliation, append-only audit state, v119-v121 Bybit persistence/control slices, security CI, signed provenance/SBOM и существенные fault tests.

Проблема находится на другом уровне: локально правильные компоненты пока не всегда соединены в **обязательную сквозную цепочку**, поэтому fresh adversarial acceptance воспроизводит ошибки на стыках data -> risk -> OMS -> broker -> executions -> accounting -> recovery.

Текущая формула состояния проекта:

> **component engineering quality != integrated product readiness**

При этом текущий fail-closed статус live/mainnet правильный и должен сохраняться.

## 2. Что реально доказано

### 2.1 Exact-main CI

Для `ff684ab85b92151c215c7d5cc85bfc527fecb5eb` подтверждено:

- canonical security run `34056249717`;
- PostgreSQL `16.15`;
- focused regression: **298 passed**;
- full regression: **1138 passed, 2 skipped**;
- два skip принадлежат выделенной fleet-deployment границе;
- canonical deployment run `34056249729` — SUCCESS;
- release provenance run `34056249725` — SUCCESS;
- release artifact id `9996047662`;
- input digest `sha256:361f1aff12d5d7661a3783be879fb02ee21b16c89c1af613e7e178e714b912d4`;
- signed provenance/SBOM flow присутствует.

Это серьёзное доказательство covered engineering/release surfaces, но **не** доказательство connected broker, protected Demo ENTRY, полного runtime, soak или profitability.

### 2.2 Fresh product acceptance

На exact source artifact 7.39.0 дополнительно проведён независимый red-team проход:

- 55 selected existing test modules: **715 passed**;
- первоначальный adversarial suite: **17 cases = 13 failed / 4 passed**;
- результат повторён 3 раза;
- отдельный F13-F17 suite: **5 нарушений из 5**, повторён **3/3**;
- F19/F20/F21/F26 воспроизведены отдельно;
- broker calls в воспроизведениях — только in-process doubles;
- реальных биржевых order mutations аудит не выполнял;
- fresh full PostgreSQL product acceptance и connected broker product acceptance пока отсутствуют.

## 3. Что в проекте уже качественно

Сохранять без архитектурного «переписывания ради переписывания» нужно:

- deterministic order/client identities;
- substantial OMS state machine;
- durable SQLite/PostgreSQL state;
- GET-first ambiguity recovery;
- risk evidence journal;
- conflict-aware strict portfolio events;
- append-only operational evidence;
- v119 runtime lease / active excursion CAS;
- v120 approval/provenance/terminal persistence;
- v121 ARM/HALT control journal и least-privilege roles;
- hash-locked release dependencies;
- action pinning, dependency/security audits;
- release manifest/SBOM/provenance;
- negative live/mainnet authority flags;
- исторически разработанные Bybit readiness/session/supervisor/recovery semantics;
- научно корректное сохранение отрицательного strategy result вместо ложного объявления прибыльности.

## 4. Product findings F01-F27

### 4.1 P0 — обязательные блокеры

| ID | Контур | Что подтверждено | Что требуется |
|---|---|---|---|
| F01 | Submit concurrency | два worker могут оба дойти до broker submit | transactional winner-only claim; loser только GET/reconcile |
| F03 | Pending risk | pending/outbox не резервируют cash/notional | durable reservations в каждом admission |
| F05 | Final dispatch | поздний HALT/readiness не отзывает уже queued risk | authoritative recheck непосредственно перед POST |
| F07 | Replace risk | risk-increasing replace не проходит новый admission | повторная оценка любого увеличения риска |
| F10 | Execution/accounting | execution fact и projection могут разойтись | immutable execution inbox + restart-safe projection |
| F12 | Stream trust | rejected unauthenticated digest может попасть в dedup до trust | validated/authenticated provenance до accounting authority |
| F13 | Operational risk | enabled limits могут молча не работать при missing observations | missing required input = explicit reject |
| F16 | Data gate | quality validator может FAIL, а signal path всё равно BUY | validated operational snapshot обязателен до strategy |
| F17 | Broker ACK | materially different broker economics могут быть приняты как ACK | strict broker-response validation / UNCERTAIN |
| F19 | Immediate fill | submit FILLED меняет OMS, но не portfolio; следующий цикл покупает снова | submit fills входят в общий durable execution path до new risk |
| F20 | Intent identity | same intent_id с другими economics считается replay | divergent replay = `INTENT_ID_CONFLICT` |
| F21 | Broker cash truth | `CASH_MISMATCH` определяется, но не блокирует BUY | broker financial truth + activity recovery участвуют в admission |
| F22 | Live market data | canonical app принимает bars от caller, не владеет live feed/finalization/scheduler | realtime ingestion + finalized bars + durable watermark + one decision/bar |
| F23 | Instrument rules | нет fresh tick/lot/min-notional/status/leverage normalization | InstrumentSpec -> normalized economics -> risk/approval |
| F24 | DR | V106 проверяет декларативный backup evidence, но не делает real backup/restore | фактический isolated backup->restore->semantic verification drill |
| F25 | Broker identity | session/dispatch не связаны с expected account/environment/credential generation/release | immutable BrokerIdentity через preflight->session->dispatch |
| F26 | Paper account binding | readonly evidence не содержал account identity; тот же evidence проходил с другим account id | privacy-safe account fingerprint equality перед mutation |

### 4.2 P1 — обязательные semantic/operational разрывы

| ID | Контур | Что подтверждено | Что требуется |
|---|---|---|---|
| F02 | Cancel/replace race | возможны 2 cancel / 2 replace calls | winner-only mutation claim |
| F04 | Clock | replay timestamp может играть роль operational freshness clock | explicit `REPLAY` / `OPERATIONAL` clock |
| F06 | Risk lineage | `approved=True` не связан с exact intent economics | immutable RiskAuthorization |
| F08 | Multisymbol valuation | второй symbol может планироваться без оценки held exposure | whole-portfolio valuation snapshot |
| F11 | Genesis | один event history можно replay с другим opening cash | immutable account/session genesis |
| F14 | Invalid data | naive last timestamp может дать TypeError вместо quality FAIL | deterministic fail-closed validation |
| F15 | Strategy qualifier | `qualified=true` при 0 trades / 0 active windows | non-vacuous policy minima |
| F18 | Broker order lineage | reconciliation может заменить broker id без replace lineage | immutable primary id или explicit successor chain |
| F27 | Point-in-time research | historical request использует `adjustment=all`, но не фиксирует `asof` | immutable asof/entity/universe/corporate-action policy |

### 4.3 P2

- **F09:** `target_quantity=0` принимается ProductConfig и отклоняется позднее strategy constructor. Validation должна быть в одной точке.

## 5. Критические E2E-разрывы

### 5.1 F19 — immediate fill создаёт риск повторного входа

Воспроизведено:

```text
after_submit_oms FILLED 1
after_submit_ledger_position 0 cash 10000
next_target 1 new_intent ('BUY', 1) approved True
```

OMS знает об исполнении, portfolio — нет, strategy видит нулевую позицию и готовит второй BUY. Это прямой reason считать execution/accounting convergence P0.

Целевой путь:

```text
validated broker execution
-> immutable execution fact
-> projection PENDING
-> portfolio/cash/fees/funding projection
-> projection receipt
-> OMS cumulative convergence
-> reconciliation
-> new risk allowed
```

Если projection падает — execution остаётся durable, а new risk блокируется.

### 5.2 F21 — broker cash truth не является risk truth

Воспроизведено:

```text
broker cash = 100
internal cash = 1000
reconcile -> CASH_MISMATCH, cash_delta = -900
next plan -> BUY 5 @ 100, approved=True
```

Следовательно reconciliation пока может быть диагностикой, а не обязательной admission boundary.

Дополнительно current recovery-reader получает только Alpaca `FILL` activities. Для полноценного account truth нужны cash-impacting activities: fees, deposits/withdrawals, dividends, interest и прочие поддерживаемые типы.

### 5.3 F22 — canonical app пока не автономный trading application

`PaperCycleService.plan_and_prepare()` принимает готовые `Sequence[Bar]` от caller. В `app/marketdata` canonical surface состоит из historical/research acquisition + validation; нет integrated realtime feed, bar finalizer, gap repair и decision scheduler.

Поэтому продукт пока не владеет контрактом:

```text
provider event
-> normalized market event
-> finalized bar
-> continuity/freshness
-> exactly-one decision
```

Это принципиальный разрыв между «библиотекой торговых компонентов» и «полноценным приложением».

### 5.4 F23 — risk должен оценивать executable economics

Для Bybit актуальные instrument rules включают tick size, quantity step, min qty, min notional, max qty, status и leverage limits. Эти значения могут изменяться.

Целевая последовательность:

```text
fresh InstrumentSpec
-> deterministic normalization
-> exact executable economics
-> risk admission
-> immutable authorization
-> final spec revalidation
-> broker submit
```

Risk на произвольных decimals с последующим broker-side normalization недостаточен.

### 5.5 F24 — DR framework не равен реальному restore

V106 содержит сильные модели `BackupManifestV106`, `RestoreEvidenceV106`, RPO/RTO/LSN/integrity checks. Но это evaluator полученных полей, а не actuator, который:

1. делает backup trading PostgreSQL state;
2. восстанавливает его в isolated target;
3. сверяет OMS/risk/portfolio/execution/control/session state;
4. запускает restored app в `HALTED / RECONCILIATION_REQUIRED`.

Для production именно этот реальный drill должен стать evidence source для V106.

### 5.6 F25/F26 — environment правильный, account может быть неправильным

Canonical product должен доказывать не только Paper/Demo endpoint, но и:

- expected account/UID identity;
- credential namespace/purpose;
- credential generation;
- release identity;
- session identity;
- same identity immediately before mutation.

Concrete F26 обнаружил, что legacy external Paper readonly evidence не включал account identity. Bounded draft PR **#146** уже добавляет privacy-safe account fingerprint и блокирует mismatch до submit/replace/cancel.

#146 не закрывает F25 полностью: session/final-dispatch binding ещё нужен во всём canonical product и Bybit path.

## 6. Research / profitability

### 6.1 Qualification contract

Fresh F15:

- `qualified=True`;
- `total_trades=0`;
- `active_windows=0`;
- `reasons=()`;
- `mean_oos_return=0`.

Следовательно qualifier должен требовать минимальную торговую активность, sample/regime coverage, costs, OOS/holdout и sensitivity.

### 6.2 Point-in-time history — F27

Historical Alpaca request сейчас:

- default `adjustment="all"`;
- не фиксирует `asof`.

Для frozen qualification это создаёт зависимость symbol/entity mapping от даты повторной загрузки. Нужно явно фиксировать `asof`, entity/universe identity и coherent corporate-action mode:

- raw bars + explicit corporate actions; либо
- adjusted analytical series с запретом double counting.

### 6.3 Текущая стратегия

Frozen Bybit price-only evidence остаётся отрицательным:

- 2,394 eligible signals;
- 792 plan eligible;
- 617 first-touch episodes;
- TARGET_FIRST 137 / STOP_FIRST 471 / NEITHER 9;
- 102 trades;
- 36 WIN / 11 BE / 55 LOSS;
- около `-176.67 USDT` на `1000 USDT` reference equity.

Статус **`PROFITABILITY_NOT_PROVEN`** сохраняется. Новая research работа должна получать новую frozen identity, а не переписывать отрицательное evidence.

## 7. Release/installability

Exact wheel/sdist 7.39.0 не являются полностью self-contained application release:

- core Postgres defaults ссылаются на `migrations/product/001..004`, отсутствующие в artifact;
- current Bybit v119-v121 Python modules ship без соответствующих root migrations;
- release qualification не делает clean install wheel без source checkout;
- installed CLI surface в основном engineering/audit/platform, а не единый product operator/service;
- sdist test surface зависит от omitted helpers.

Issue **#137** должен превратить release artifact в самодостаточный runtime package.

Дополнительный identity debt: release `7.39.0`, а Alpaca adapter всё ещё отправляет исторический `User-Agent: ASTRA/7.30.0 paper-only`. Это не P0 trading defect, но runtime outward identity должна выводиться из одной release identity, а не из захардкоженной старой строки.

## 8. Runtime / observability / operations

`OperationalReadinessEvaluator` — полезный компонент, но evaluator сам по себе не является control plane.

Для production нужен authoritative assembler, который собирает actual:

- market-data quality/freshness;
- feed/stream state;
- broker/account status;
- OMS uncertain/reconciling states;
- reservations;
- execution/accounting lag;
- whole-portfolio valuation;
- session PnL/drawdown;
- HALT/ARM;
- DB/storage health;
- release/account/credential identity;
- backup freshness.

Именно этот snapshot должен проверяться **в final dispatch boundary**.

Также необходимы:

- structured logs/correlation ids;
- metrics/alerts;
- long-running worker/service lifecycle;
- graceful shutdown;
- restart recovery;
- operator CLI/API;
- runbooks;
- actual backup/restore drills.

Issue **#140** остаётся P0.

## 9. Canonical Bybit completion

Current `main` содержит strategy-free v119-v121 slices, а не полный connected Bybit product.

Bounded canonicalization ещё должна безопасно соединить:

- v122 restart-safe session risk;
- one-time session start;
- terminal evidence -> risk commit -> checkpoint ACK;
- active-trade supervisor;
- v123 audited lease recovery;
- connected read-only preflight;
- trading-key metadata proof;
- fixed-egress trust zone;
- v124 logical DB/bootstrap/readiness;
- exact-symbol approval lineage;
- protected Demo ENTRY;
- protection/reduce-only recovery.

Но completion этих slices **не отменяет F01-F27**. Инфраструктурная extraction может продолжаться только с честным названием capability, без заявления готовности продукта.

## 10. Текущая E2E-матрица

| Контур | Статус |
|---|---|
| Historical point-in-time data | FAIL — F27 |
| Realtime data -> finalized bar -> decision | FAIL — F04/F14/F16/F22 |
| Strategy generation | COMPONENT PASS / validation-only |
| Strategy qualification/promotion | FAIL — F15 |
| Profitability | FAIL — `PROFITABILITY_NOT_PROVEN` |
| Instrument normalization | FAIL — F23 |
| Risk admission | FAIL — F03/F06/F07/F13/F21 |
| Portfolio/account financial truth | FAIL — F03/F08/F11/F21 |
| OMS identity/concurrency | FAIL — F01/F02/F20 |
| Submit | FAIL — F01/F05/F17/F19/F25 |
| Cancel/replace | FAIL — F02/F07/F18/F25 |
| Broker-event trust | FAIL — F12 |
| Execution/accounting convergence | FAIL — F10/F19/F21 |
| Account/environment identity | FAIL — F25; F26 repair in #146 |
| Runtime service/telemetry | FAIL — #140 |
| Release installability | FAIL — #137 |
| Real backup/restore | FAIL — F24/#144 |
| Canonical connected Bybit runtime | NOT PRESENT YET |
| Protected Demo ENTRY | NOT PROVEN |
| Connected soak | NOT PROVEN |
| Server-side governance | FAIL — #103 |
| Live/mainnet | FAIL-CLOSED |

## 11. Backlog, синхронизированный с ТЗ

### P0

- #134 — bounded F12 repair;
- #135/#138 — durable execution facts + accounting convergence;
- #138 — immutable economic identity / immediate fills / broker ACK lineage;
- F01/F02 — exclusive external-mutation claims;
- F03 — pending reservations;
- F06/F07 — exact risk authorization and amendment re-admission;
- #136 — mandatory data/risk fail-closed gates;
- #141 — broker financial activities and cash truth;
- #142 — realtime market-data ingestion/bar scheduler;
- #143 — InstrumentSpec normalization;
- #145 + #146 — broker/account/credential identity;
- #140 — authoritative runtime snapshot/final dispatch/telemetry;
- #137 — self-contained release;
- #144 — real trading-state backup/restore;
- #103 — server-side branch protection.

### P1

- F04 clock modes;
- F08 whole-portfolio valuation;
- F11 immutable genesis;
- #139 strategy qualification/promotion;
- #147 point-in-time research/corporate actions;
- #109 physical append-only hardening;
- exact-head versus synthetic-merge evidence identity;
- remaining bounded Bybit canonicalization;
- connected soak and production security/DR evidence.

### P2

- F09 ProductConfig positive quantity;
- runtime User-Agent/release identity drift;
- stale stacked PR retirement only after preservation decisions.

## 12. Приоритетный порядок работ

```text
trusted broker/execution/accounting truth
F12 -> F10/F19/F21 -> F17/F18/F20 -> F26
        ↓
exclusive mutations + reservations + exact risk lineage
F01/F02/F03/F06/F07
        ↓
operational input and dispatch truth
F13/F14/F16/F22/F23/F25/F05
        ↓
portfolio/time/research semantics
F04/F08/F09/F11/F15/F27
        ↓
self-contained release + service + telemetry + real DR
#137/#140/#144
        ↓
remaining bounded Bybit canonicalization
        ↓
one exact-SHA PostgreSQL/fault product qualification
        ↓
protected connected Demo chain
        ↓
connected soak
        ↓
separate frozen positive strategy qualification
        ↓
server governance/security/DR
        ↓
separately approved tiny-capital live pilot
```

## 13. Definition of Done

`PRODUCT_READY` разрешён только когда одновременно:

1. F01-F27 закрыты либо формально superseded более сильным доказанным contract.
2. Race/crash P0 проходят SQLite/PostgreSQL/multiprocess fault tests.
3. Operational data, risk context, instrument spec и account identity fail closed.
4. Pending reservations входят в risk truth.
5. Broker executions и financial activities являются immutable source of truth.
6. Accounting projection restart-safe и convergent.
7. Intent/order/execution identities conflict-aware.
8. Final readiness/HALT проверяется непосредственно перед risk-increasing mutation.
9. Installed artifact самодостаточен.
10. Реальный backup/restore drill доказан.
11. Canonical Bybit runtime собран на `main`.
12. Одна exact release identity проходит полный deterministic/PostgreSQL/security/fault product gate.
13. Protected connected Demo chain PASS.
14. Connected soak PASS.
15. Positive frozen cost-adjusted OOS strategy evidence существует отдельно.
16. Server-side governance/security/observability/DR PASS.
17. Independent live approval остаётся отдельным gate.

До этого корректное описание проекта:

> **Сильная инженерная торговая платформа с существенными квалифицированными компонентами. Полноценный production trading product ещё не доказан; live/mainnet правильно остаётся FAIL_CLOSED.**
