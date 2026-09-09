# Полный аудит продукта ASTRA / trade_bot

Дата: 2026-09-09  
Объект: `PetrFedin/trade_bot`  
Exact SHA: `ff684ab85b92151c215c7d5cc85bfc527fecb5eb`  
Статус: **NOT_PRODUCT_READY**

## 1. Итог

Проект нельзя честно назвать законченным production trading product.

При этом нельзя и обесценивать уже сделанное: в репозитории есть сильный инженерный фундамент — durable OMS, PostgreSQL/SQLite persistence, mutation/reconciliation semantics, fail-closed проверки, release provenance/SBOM, значительный набор fault/security tests, Bybit Demo operational semantics в историческом стеке и серьёзный research-контур.

Главная проблема в другом: **качество отдельных компонентов заметно выше качества их текущей канонической сборки как одного продукта**.

На текущем `main` одновременно верны две вещи:

1. покрытые deterministic/PostgreSQL/release поверхности имеют реальное зелёное CI-доказательство;
2. свежая сквозная product-acceptance проверка воспроизводит критические межмодульные дефекты, а полноценная Bybit operational composition вообще ещё не канонизирована.

Следовательно, зелёный CI нельзя использовать как утверждение «приложение полноценно работает».

## 2. Что проверено

### 2.1 Канонический GitHub state

Проверено:
- `main = ff684ab85b92151c215c7d5cc85bfc527fecb5eb`;
- `main` не защищён server-side;
- C2B0 уже слит в `main`;
- PR #131, который должен синхронизировать status/docs после C2B0, остаётся открытым;
- issue #130 описывает следующий C2B1 v122 persistence slice;
- issue #109 по физическому TRUNCATE hardening v107-v109 открыт;
- issue #104 по консолидации огромного stacked graph открыт;
- создан новый product acceptance blocker #132.

### 2.2 Официальное CI-доказательство текущего main

Для exact `ff684...` подтверждено существующее post-merge evidence:
- canonical security PostgreSQL 16.15;
- focused regression: **298 passed**;
- full regression: **1138 passed, 2 dedicated fleet-deployment skips**;
- отдельный canonical deployment workflow для fleet boundary — success;
- release provenance — success;
- signed SLSA/SBOM evidence присутствует в release flow.

Это реальное доказательство качества соответствующих компонентов. Оно не доказывает соединённый брокер, end-to-end trading product или прибыльность.

### 2.3 Fresh product acceptance

На релизном source artifact текущего main выполнен дополнительный приёмочный прогон:
- 55 выбранных существующих test modules: **715 passed**;
- отдельный adversarial product-acceptance набор: **17 cases = 13 failed + 4 passed**;
- финальный результат воспроизведён три раза;
- без реальной биржевой мутации;
- SQLite + in-process broker doubles;
- это не новый PostgreSQL qualification и не connected broker evidence.

### 2.4 Release artifact

Проверены wheel/sdist из release provenance run `34056249725`.

Установочная поверхность содержит Python-код и platform SQL v105-v109, но не содержит canonical root migrations v119/v120/v121, хотя текущие Bybit persistence модули и тесты на них ссылаются.

Fresh pristine-sdist targeted run:
- **16 passed**;
- **2 failed**;
- **2 skipped**;
- оба FAIL — отсутствие `migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql`.

Это означает: квалифицированный artifact пока не является самодостаточной установочной единицей для текущей Bybit persistence lineage.

### 2.5 Архитектура main

Canonical `ProductRuntime` сейчас собирает:
- `LongOnlyMomentumStrategy`;
- `PreTradeRiskEngine`;
- risk admission/journal;
- durable OMS + mutations;
- portfolio;
- paper lifecycle/reconciler;
- optional fill accounting;
- `OperationalReadinessEvaluator`.

Это сильная paper-oriented library composition.

В текущем main Bybit слой состоит в основном из strategy-free v119-v121 persistence/control contracts. Полный Bybit broker/client/session/supervisor/recovery/approval service находится в историческом stacked operational source, а не в каноническом продукте.

## 3. Критические воспроизведённые дефекты

### P0 / F01 — duplicate external submit under concurrency

Текущий idempotent transition в `SUBMIT_STARTED` не является эксклюзивным claim.

Два worker могут:
1. прочитать один OUTBOXED intent;
2. оба вызвать одинаковый idempotent transition;
3. второй получает уже существующее `SUBMIT_STARTED` состояние;
4. оба продолжают к broker call.

В тестовом broker-double наблюдалось **2 outbound submit invocations**.

Это не доказательство двух биржевых ордеров, но это нарушение at-most-once external mutation contract.

**Что нужно:** отдельный transactional claim, который возвращает `CLAIMED` только одному worker. Все остальные — GET/reconcile only. Claim должен иметь crash recovery semantics.

### P1 / F02 — duplicate cancel/replace under concurrency

Аналогичный дефект присутствует в mutation lifecycle: одинаковый start event не даёт одному worker эксклюзивное право на broker mutation.

**Что нужно:** тот же winner-only mutation claim для cancel/replace.

### P0 / F03 — pending orders do not reserve capital/exposure

Risk admission считает текущие позиции/капитал, но не durable pending/outbox exposure.

В воспроизводимом сценарии три заявки по 102 каждая проходят против cash/notional cap 110, т.е. queued risk = 306.

**Что нужно:** durable reservation ledger, включаемый в every new-risk admission; release/adjust on fill/cancel/reject/reconcile.

### P1 / F04 — implicit clock can classify historical data as fresh

Когда operational caller не передаёт явный risk/clock context, pipeline может использовать timestamp решения самой historical bar как reference time.

Для replay это допустимый контракт. Для real-time path это опасная двусмысленность.

**Что нужно:** разные typed modes `REPLAY_CLOCK` и `OPERATIONAL_CLOCK`; operational entry нельзя собирать без exchange/server/wall clock freshness context.

### P0 / F05 — readiness/HALT is not a final dispatch capability

В canonical paper composition readiness evaluator существует отдельно от внешней mutation boundary. Уже outboxed order не получает durable revoke/final guard только из-за того, что новая planning attempt blocked.

**Что нужно:** durable HALT/KILL/readiness capability, повторно проверяемая непосредственно перед risk-increasing external call. Risk-reducing recovery при этом должна оставаться доступной.

### P1 / F06 — approved RiskDecision is not bound to exact order intent

Нижний `PaperOrderLifecycle` принимает `RiskDecision` как approved/not approved без exact identity связи с intent economics.

Верхний planner обычно генерирует корректную пару, но lower boundary нельзя считать безопасным самостоятельно.

**Что нужно:** `RiskAuthorization` с `intent_id`, canonical economics hash/revision, decision id и immutable evidence identity.

### P0 / F07 — replace can increase risk without new admission

Replace path валидирует order semantics, но не пересчитывает current + reserved exposure против новых economics.

В acceptance case replace на 100000 дошёл до broker-double при configured cap 110.

**Что нужно:** every risk-increasing amend проходит новый admission; risk-decreasing amend может иметь упрощённый путь.

### P1 / F08 — multisymbol portfolio valuation incomplete

Pipeline формирует price map только для symbol нового target. Если уже есть позиция по другому symbol, gross-notional valuation требует цену для неё и может падать.

**Что нужно:** отдельный portfolio valuation snapshot по всем positions + reservations с freshness contract.

### P2 / F09 — config invariant split

`ProductConfig.validate()` принимает `target_quantity=0`, а strategy constructor требует положительное значение.

**Что нужно:** один контракт > 0 на config boundary.

### P0 / F10 — factual fill and accounting projection can diverge

Fill accounting может:
1. записать durable portfolio event;
2. затем не применить runtime ledger из-за cash/fee invariant;
3. не довести OMS к тому же состоянию;
4. получить историю, которую следующий replay не может корректно спроецировать.

Нельзя исправлять это простым отказом от фактического broker fill.

**Правильный дизайн:** immutable broker execution inbox -> accounting projection state machine -> OMS/accounting reconciliation. Если projection invalid, factual fill остаётся durable, система блокирует новый риск и входит в recoverable `ACCOUNTING_PENDING`/quarantine.

### P1 / F11 — opening cash is not part of immutable portfolio identity

Persisted event history может быть replayed с другим произвольным `opening_cash` argument.

**Что нужно:** immutable session/account genesis record и explicit cash-flow events. History identity обязана включать genesis.

### P0 / F12 — unauthenticated stream frame can gain trusted dedup provenance

`AlpacaPaperTradeUpdateStream.ingest` добавляет digest в seen-set до полной авторизации/semantic validation. Outer processor затем независимо парсит raw fill.

Результат воспроизводимого сценария:
- первое unauthenticated событие rejected/quarantined, но digest сохранён;
- exact replay воспринимается stream как duplicate и возвращает `None`;
- outer exact-fill parser продолжает accounting;
- stream остаётся quarantined, а portfolio position меняется.

**Что нужно:** digest становится trusted only after successful stream semantic validation. При этом retry после downstream accounting failure должен оставаться возможным, поэтому требуется explicit validated-frame provenance, а не простое «не обрабатывать duplicates».

## 4. Что работает качественно

Следующие области нельзя считать дефектными только потому, что весь продукт ещё не готов:

- deterministic client/order identities;
- значительная часть OMS durability;
- append-only persistence;
- PostgreSQL least-privilege work v119-v121;
- active-excursion CAS;
- ambiguity handling в последовательном single-worker сценарии;
- release action pinning / dependency/security controls;
- signed release provenance/SBOM;
- существующие negative safety constraints для live/mainnet;
- исторически разработанные Bybit fixed-egress / ARM/HALT / session risk / supervisor / recovery semantics;
- research discipline: retrospective evidence не выдаётся за live edge.

Fresh positive control cases подтвердили:
- normal plan -> submit -> fill -> duplicate -> restart;
- sequential ambiguous-submit GET recovery;
- explicitly stale RiskContext rejection;
- disabled broker blocks submit.

## 5. End-to-end оценка

| Контур | Статус | Комментарий |
|---|---|---|
| Market data validation | PARTIAL PASS | базовые contracts сильные; operational clock split не завершён |
| Signal generation | COMPONENT PASS | paper/research реализация есть |
| Strategy profitability | FAIL | frozen Bybit evidence отрицательное |
| Risk admission | PARTIAL FAIL | reservations, exact authorization, replace re-admission |
| OMS persistence | STRONG COMPONENT PASS | но claim semantics не защищают внешний POST |
| Submit E2E | FAIL | F01/F05 |
| Cancel/replace E2E | FAIL | F02/F07 |
| Fill ingestion | FAIL | F12 |
| Accounting convergence | FAIL | F10/F11 |
| Multisymbol portfolio | FAIL | F03/F08 |
| Restart/recovery components | STRONG | integrated product acceptance не завершён |
| Canonical Bybit connected product | NOT PRESENT | только часть slices в main |
| Historical Bybit operational code | CODE-QUALIFIED SOURCE | не canonical, не connected-evidence PASS |
| Release artifact | PARTIAL FAIL | PKG01 migrations missing |
| Observability | PARTIAL | evaluator/logging pieces есть, runtime metrics/alert gate неполный |
| Deployment/DR | PARTIAL/UNPROVEN | release provenance есть; production restore/soak absent |
| Governance | FAIL | main protection disabled |
| Demo | NOT PROVEN | no real canonical protected entry |
| Live | FAIL-CLOSED | правильно заблокирован |

## 6. Расхождение с заявленной целью «не demo/MVP»

Сейчас проект больше похож на **advanced trading-system engineering platform + incubator**, чем на единый продукт.

Причины:
- canonical main не содержит полного Bybit runtime;
- историческая реализация размазана по десяткам stacked PR;
- research и operational semantics физически существуют, но не образуют один release unit;
- нет одного официального service entrypoint для торгового приложения;
- `build_postgres_product`/`PaperCycleService` фактически являются library/test composition;
- readiness не является последним mutation gate;
- release artifact не содержит часть migrations;
- alerting/dashboard/production run loop/DR evidence не образуют один завершённый operational contract;
- connected Demo chain и soak отсутствуют.

Поэтому правильная цель следующего этапа — не добавлять ещё функции, а **свести имеющиеся функции в один канонический путь и закрыть выявленные межмодульные инварианты**.

## 7. Приоритет исправлений

### Wave P0-A — доверие к внешним фактам

1. F12 stream trust/provenance.
2. F10 execution inbox + accounting projection convergence.

Пока эти два пункта не закрыты, broker event boundary нельзя считать надёжным.

### Wave P0-B — единственность внешней мутации

3. F01 exclusive submit claim.
4. F02 mutation claim.
5. multiprocess/PostgreSQL/crash-after-claim campaign.

### Wave P0-C — portfolio risk before broker

6. F03 durable reservations.
7. F05 durable final HALT/readiness gate.
8. F06 exact risk lineage.
9. F07 risk-increasing amendment re-admission.
10. F08 whole-portfolio valuation.

### Wave P1-D — financial identity/release integrity

11. F11 immutable account/session genesis.
12. F09 config invariant.
13. PKG01 package v119-v124 migrations/assets into installed release.
14. pristine artifact bootstrap/test.

### Wave P1-E — canonical Bybit product

Только после P0-A/B/C:
- C2B1 v122 persistence;
- C2B2 one-time session start;
- C2B3 terminal evidence -> risk -> checkpoint;
- C2C supervisor/recovery;
- C1A connected identity/fixed-egress;
- C1B v124 final bootstrap/readiness;
- C3A approval lineage;
- one official composition/service.

Не переносить исторические PR wholesale. Извлекать bounded semantics и тесты на текущий main.

### Wave P1-F — production operation

- integrated metrics/tracing/alerts;
- runbook-linked alert policies;
- backup/restore drill;
- production identity/secrets/KMS;
- protected main;
- exact-head connected Demo chain;
- multi-week soak;
- fault campaign.

### Separate alpha track

Стратегия развивается независимо. Нельзя разрешать инженерную готовность компенсировать отрицательное математическое ожидание.

## 8. Документация и source-of-truth

Текущие документы не полностью синхронны:
- `main` фактически уже на C2B0 `ff684...`;
- `CURRENT_SYSTEM_STATUS.json` в main всё ещё описывает C2A4/C2B0-in-progress;
- README ещё старее;
- PR #131 пытается исправить только C2B0/C2B1 status, но был создан до свежего product acceptance #132.

Следовательно PR #131 **нельзя считать достаточной финальной синхронизацией**. После утверждения product audit он должен быть либо обновлён с новым blocker/status contract, либо заменён новым status-only PR.

## 9. Что доказано

Доказано:
- текущий main exact SHA известен;
- covered engineering/security/release CI на нём реально зелёное;
- main protection реально disabled;
- стратегия по frozen price-only evidence отрицательная;
- canonical Bybit full runtime в main отсутствует;
- релизный package не содержит текущие root migrations;
- свежие cross-module acceptance counterexamples воспроизводятся;
- новый P0 blocker #132 необходим.

## 10. Что НЕ доказано

Не доказано:
- что любой из F01-F12 привёл к фактическому ущербу на реальной бирже;
- что реальный Bybit Demo account сейчас подключён;
- что protected fixed-egress environment существует и operationally qualified на текущем main;
- что реальный Demo ENTRY был выполнен;
- что full exact-head broker chain PASS;
- что connected soak PASS;
- что production restore/KMS/alerts PASS;
- что стратегия прибыльна;
- что mainnet готов.

## 11. Финальный вердикт

### Инженерная платформа

**Сильная.** Архитектурная зрелость выше типичного MVP: есть durable state, fail-closed мышление, evidence lineage и серьёзная release/security дисциплина.

### Текущий канонический продукт

**Не завершён.** Главный deficit — не количество функций, а несколько критических межмодульных контрактов и отсутствие единой канонической Bybit composition.

### Demo

**Не доказано.** Код исторического operational stack не равен реальному connected Demo proof.

### Production

**Нет.** P0/P1 acceptance, governance, connected evidence, soak и DR не закрыты.

### Profitability

**Нет.** Frozen evidence отрицательное; статус `PROFITABILITY_NOT_PROVEN` должен оставаться.

## 12. Следующая точка разработки

Следующий кодовый PR должен быть не C2B1 и не новый feature. Самая опасная текущая граница — **F12 validated stream provenance -> accounting**.

Рекомендуемый bounded change:
1. сделать trusted dedup только после полного protocol/state validation;
2. сохранить возможность retry validated frame после downstream accounting failure;
3. запретить outer accounting для frame, не имеющего accepted-stream provenance;
4. добавить unauthenticated duplicate, authorized duplicate, downstream-failure replay, out-of-order and quarantine recovery tests;
5. прогнать full regression;
6. только после merge перейти к F10 execution inbox/accounting convergence.

Именно такой порядок уменьшает реальный operational risk быстрее, чем дальнейшее наращивание исторической feature stack.
