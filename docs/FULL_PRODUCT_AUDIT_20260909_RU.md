# Полный аудит продукта ASTRA / trade_bot

Дата: 2026-09-09  
Объект: `PetrFedin/trade_bot`  
Базовый exact SHA: `ff684ab85b92151c215c7d5cc85bfc527fecb5eb`  
Статус приёмки: **NOT_PRODUCT_READY**

## 1. Итоговый вывод

ASTRA уже содержит сильную инженерную платформу, но на текущем каноническом `main` ещё не является одним законченным production trading product.

Это не вывод по количеству файлов или незавершённых задач. Он основан на трёх одновременно подтверждённых фактах:

1. **Компонентный фундамент действительно сильный.** На текущем `main` есть durable OMS, SQLite/PostgreSQL persistence, append-only evidence, mutation/reconciliation logic, risk evidence, release provenance/SBOM, substantial fault/security tests и качественно извлечённые strategy-free Bybit v119-v121 persistence/control slices.
2. **Свежая сквозная приёмка воспроизводит межмодульные нарушения безопасности и целостности**, которые не опровергаются зелёным component CI. Аудит расширен с F01-F12 до F01-F20.
3. **Канонический продукт пока не соединяет research, data quality, risk, execution, accounting, operations и Bybit operational plane в один обязательный runtime path.** Полная Bybit session/supervisor/recovery/readiness/approval composition ещё находится в historical stacked source, а не в одном релизном продукте.

Ключевая формула аудита:

> `component engineering quality != integrated product readiness`

Поэтому корректное позиционирование на сегодня: **advanced fail-closed trading-system engineering platform с сильными квалифицированными компонентами, но ещё не завершённый production trading product**.

## 2. Что проверено

### 2.1 GitHub и source-of-truth

Проверено состояние канонического репозитория и активного backlog:

- `main = ff684ab85b92151c215c7d5cc85bfc527fecb5eb`;
- C2B0 уже в `main`;
- PR #131 с прежней status-sync логикой остаётся открытым и сам по себе уже недостаточен после fresh product audit;
- issue #130 описывает C2B1 v122 persistence slice;
- #103 — server-side `main` protection;
- #104 — stacked-PR consolidation;
- #109 — physical TRUNCATE/runtime ownership hardening v107-v109;
- #132 — parent product-acceptance blocker;
- #135 — F10 durable broker execution facts/accounting projection;
- #136 — market-data + operational risk fail-closed admission;
- #137 — self-contained installed release;
- #138 — immutable execution/OMS economic identity + convergence;
- #139 — strategy qualification/promotion contract;
- #140 — authoritative runtime health/telemetry/final dispatch service.

### 2.2 Официальное CI-доказательство текущего main

Для exact `ff684...` подтверждено существующее post-merge evidence:

- canonical security run `34056249717`;
- PostgreSQL `16.15`;
- focused security regression: **298 passed**;
- full regression: **1138 passed, 2 skipped**;
- два skip относятся к отдельной fleet-deployment границе; canonical deployment run `34056249729` — success;
- release provenance run `34056249725` — success;
- release artifact id `9996047662`;
- release input digest `sha256:361f1aff12d5d7661a3783be879fb02ee21b16c89c1af613e7e178e714b912d4`;
- signed SLSA/SBOM присутствуют в trusted release flow.

Это реальное доказательство качества соответствующих deterministic/PostgreSQL/release surfaces. Оно **не является** доказательством connected broker, Demo ENTRY, полного runtime, soak, production или прибыльности.

### 2.3 Fresh local product acceptance

На exact release source artifact версии `7.39.0` дополнительно выполнена сквозная проверка:

- 55 выбранных existing test modules: **715 passed**;
- исходный adversarial acceptance набор: **17 cases = 13 failed + 4 passed**;
- этот результат воспроизведён в трёх финальных повторениях;
- дополнительный F13-F17 adversarial набор: **5 нарушений из 5**, повторён **3/3**;
- F19 immediate-fill convergence и F20 intent identity воспроизведены отдельно;
- Python 3.13.5 / pytest 9.0.2 / SQLite + in-process broker doubles;
- это не новый PostgreSQL qualification и не connected broker evidence;
- реальных биржевых order mutations аудит не выполнял.

### 2.4 Полнота source/package поверхности

Из exact sdist проверены все поставляемые application domains. Структура substantial:

- application;
- domain;
- marketdata;
- strategy;
- risk;
- OMS;
- portfolio/accounting;
- execution;
- observability;
- runtime/platform layers.

Полное Python-дерево sdist компилируется без syntax errors. Direct test-import density высокая: проблема проекта не в том, что тестов мало. Основные ошибки находятся **на стыках локально зелёных компонентов**.

### 2.5 Release artifact как устанавливаемый продукт

Exact wheel/sdist не являются самодостаточными для всех объявленных runtime paths:

- core PostgreSQL stores по умолчанию ссылаются на `migrations/product/001..004`, которых нет в wheel/sdist;
- canonical Bybit v119-v121 Python persistence/control modules поставляются без root migrations v119/v120/v121;
- source distribution содержит тесты, импортирующие отсутствующие `tests.conftest` / `tests.helpers_v108`;
- release flow доказывает build/hash/SBOM/provenance, но не выполняет clean install built wheel -> packaged schema resources -> DB verify/bootstrap -> offline product self-test;
- установленная CLI-поверхность в основном состоит из engineering/audit/platform commands, а не одного официального product/operator interface.

Это зафиксировано в #137.

## 3. Что работает качественно и должно быть сохранено

Нельзя смешивать product FAIL с отрицанием сильных частей проекта. Хорошо реализованы и/или хорошо доказаны:

- deterministic identities и substantial OMS state machine;
- SQLite/PostgreSQL durability;
- GET-first ambiguity recovery в последовательном single-worker сценарии;
- risk evidence journal;
- conflict-aware strict portfolio events;
- append-only evidence patterns;
- v119 runtime lease / active excursion CAS;
- v120 audit persistence и hardening;
- v121 control journal / least-privilege roles;
- release action pinning, dependency security, lock/provenance/SBOM;
- негативные safety flags для live/mainnet;
- исторически разработанные Bybit fixed-egress, ARM/HALT, session-risk, supervisor и recovery semantics;
- research discipline: отрицательный retrospective result не объявлен прибыльной стратегией.

Положительные local controls подтвердили:

- normal plan -> submit -> fill -> duplicate -> restart;
- sequential ambiguous-submit GET recovery;
- explicitly stale `RiskContext` rejection;
- disabled broker blocks submit.

Эти свойства нужно **сохранить**, а не переписывать ради упрощения.

## 4. Воспроизведённые product findings F01-F20

### 4.1 P0 — обязательные блокеры

| ID | Контур | Подтверждённый дефект | Требуемое исправление |
|---|---|---|---|
| F01 | external submit | два worker могут оба получить фактически допустимый `SUBMIT_STARTED` и оба вызвать broker submit | transactional winner-only claim; loser имеет только GET/reconcile capability |
| F03 | pending risk | pending/outbox заявки не резервируют cash/notional; 3 x 102 прошли против лимита 110 | durable reservation ledger, участвующий во всех новых admissions |
| F05 | final dispatch | readiness/HALT не являются обязательным last-mile gate для уже outboxed order | повторная authoritative проверка HALT/readiness непосредственно перед risk-increasing POST |
| F07 | replace risk | replace на 100000 достиг broker-double при лимите 110 | every risk-increasing amend получает новый risk admission |
| F10 | accounting | factual fill может опередить runtime projection и оставить divergent state | immutable execution inbox -> projection state machine -> OMS/accounting convergence |
| F12 | stream trust | rejected unauthenticated frame может через stream dedup приобрести accounting path | только validated/authenticated stream provenance даёт accounting authority |
| F13 | risk context | включённые operational limits могут молча не проверяться из-за missing optional observations | operational context fail-closed: missing required observation = explicit rejection |
| F16 | signal data gate | `validate_bar_series()` может отвергнуть данные, но canonical pipeline всё равно создаёт approved BUY | validated operational data snapshot обязателен до strategy/risk |
| F17 | broker ACK | broker response с materially different order economics может быть принят как ACK | strict response/economics validation; вне разрешённого venue normalization -> UNCERTAIN |
| F19 | immediate fill | submit может вернуть FILLED, OMS станет FILLED, portfolio останется 0 и следующий цикл одобрит второй BUY | submit fills обязаны идти через ту же durable execution/accounting convergence цепочку; new risk блокируется до convergence |
| F20 | intent identity | повтор того же `intent_id` с другими qty/price может считаться idempotent success | `intent_id` = immutable economic identity; divergent replay -> `INTENT_ID_CONFLICT` |

### 4.2 P1 — обязательные semantic/operational разрывы

| ID | Контур | Подтверждённый дефект | Требуемое исправление |
|---|---|---|---|
| F02 | cancel/replace | concurrency race даёт 2 cancel / 2 replace broker calls | winner-only external-mutation claim для amendments |
| F04 | clock | historical bar time может использоваться как implicit freshness clock | typed `REPLAY_CLOCK` vs `OPERATIONAL_CLOCK` |
| F06 | risk lineage | lower lifecycle принимает `approved=True` без exact binding к intent economics | immutable `RiskAuthorization` с intent/economics/decision revision |
| F08 | multisymbol | planning второго symbol не имеет price context для уже удерживаемого symbol | authoritative whole-portfolio valuation snapshot |
| F11 | account genesis | один и тот же event history можно replay с другим opening cash | immutable session/account genesis + explicit cash-flow events |
| F14 | data validation | naive timestamp последнего invalid bar может вызвать TypeError вместо `ready=false` | quality gate всегда возвращает deterministic fail-closed evidence |
| F15 | qualification | walk-forward может дать `qualified=true` при 0 trades / 0 active windows | non-vacuous minimum trades/windows/regimes/sample/cost/OOS policy |
| F18 | broker lineage | reconciliation может заменить `broker-A` на `broker-B` без explicit replace lineage | immutable primary broker ID либо доказанная `replaces/replaced_by` цепочка |

### 4.3 P2

- **F09:** `ProductConfig` принимает `target_quantity=0`, а strategy construction отклоняет. Требуется один positive-quantity contract на config boundary.

## 5. Разбор ключевых P0 глубже

### F01/F02 — idempotency не равна exclusive mutation authority

Текущее поведение защищает durable event/state от очевидного дубля, но внешний POST требует более сильного свойства.

Нужен lifecycle:

```text
OUTBOXED / MUTATION_REQUESTED
-> EXCLUSIVE_CLAIM(owner, generation/fence)
-> SUBMIT_STARTED / MUTATION_STARTED
-> one external call by claim owner only
-> broker truth | UNCERTAIN
-> GET/reconcile
```

Второй worker никогда не должен получать право на POST только потому, что видит тот же idempotent transition.

### F03 — pending risk должен быть first-class state

Экспозиция продукта = не только filled positions. Для каждого admission должны учитываться:

```text
broker positions
+ acknowledged/open orders
+ outbox/pending reservations
+ partially filled residuals
+ execution facts not yet projected
+ recovery-only exposure
```

Reservation release возможен только после доказанного fill/cancel/reject/reconciliation result.

### F05/F13/F16 — fail-closed должен быть обязательным path, а не библиотечной опцией

В репозитории существуют хорошие validators и evaluator, но caller может обойти их или передать неполный optimistic context.

Production operational path должен использовать typed authoritative input, в котором:

- enabled risk limit объявляет обязательные measured inputs;
- unknown ADTV/equity/volatility/sector/spread/slippage/session loss и др. не превращаются в zero/healthy defaults;
- invalid/stale/gapped/future data не доходят до strategy;
- current readiness/HALT перечитываются в final dispatch transaction/claim boundary.

Это #136 + #140.

### F10/F19 — broker execution fact и accounting projection должны быть разделены

Главное правило:

> факт исполнения брокера нельзя удалить, проигнорировать или переписать только потому, что внутренняя модель не смогла его применить.

Целевая схема:

```text
validated broker execution
-> immutable execution inbox
-> idempotent accounting projection
-> projection receipt/checkpoint
-> OMS cumulative truth convergence
-> broker/account reconciliation
```

Если projection не применился:

```text
execution fact = durable
projection = PENDING/QUARANTINED
new risk = BLOCKED
recovery = deterministic replay/reconcile
```

F19 показывает, что этот gate нужен не только для websocket fill. Он обязателен уже при **immediate fill в submit response**.

### F17/F18/F20 — технический ID должен защищать экономическую идентичность

Три разных дефекта имеют один корень: повтор/сопоставление часто проверяет технический ID слабее, чем economics.

Production contract должен запрещать silent drift:

- same `intent_id` + changed economics -> conflict;
- same event id + changed payload/target/broker identity -> conflict;
- established broker order id + new broker order id -> conflict, если нет exact replace lineage;
- broker ACK с другой экономикой -> uncertainty/reconciliation, а не success.

Reference pattern уже есть в `StrictPortfolioEventStore`: identical replay = idempotent; divergent replay = conflict. Этот принцип надо распространить на OMS/execution boundaries.

## 6. Strategy/research readiness

### 6.1 Текущий runtime не является promoted strategy runtime

Canonical `ProductRuntime` hardcodes `LongOnlyMomentumStrategy`.

Сам этот класс документирован как deterministic validation strategy для paper E2E vertical slice и явно не представлен как profitable strategy.

Одновременно repository содержит richer research-only components, включая regime-aware и cross-sectional logic. Но отсутствует обязательный механизм:

```text
frozen dataset/source manifest
+ frozen strategy code/config
+ frozen cost model
+ OOS/holdout evidence
-> qualification manifest
-> independent approval
-> immutable runtime strategy release identity
```

### 6.2 F15

Свежий adversarial test получил:

- `qualified=True`;
- `total_trades=0`;
- `active_windows=0`;
- `reasons=()`;
- `mean_oos_return=0`.

Это не доказывает, что текущая стратегия плохая; это доказывает, что **сам qualification contract допускает vacuous PASS**.

Требуется #139.

### 6.3 Текущая profitability truth

Отрицательное frozen Bybit price-only evidence сохраняется без изменений:

- 2,394 eligible signals;
- 792 plan eligible;
- 617 first-touch episodes;
- TARGET_FIRST 137 / STOP_FIRST 471 / NEITHER 9;
- portfolio replay 102 trades;
- 36 WIN / 11 BE / 55 LOSS;
- приблизительно `-176.67 USDT` на `1000 USDT` reference equity.

Следовательно статус остаётся **`PROFITABILITY_NOT_PROVEN`**. Engineering work не может заменить этот gate.

## 7. Release/installability

### 7.1 Что уже хорошо

- wheel/sdist build;
- hash-locked dependencies;
- dependency/security checks;
- release manifest;
- SBOM;
- signed provenance/attestation flow.

### 7.2 Что не позволяет назвать artifact законченным продуктом

1. `build_postgres_product(..., migrate=True)` использует `migrations/product/001..004`, отсутствующие в exact wheel/sdist.
2. Current Bybit v119-v121 modules также не получают corresponding root migration resources внутри installed package.
3. Resource loading опирается на repository-relative paths вместо immutable package resources.
4. Нет clean-installed-wheel qualification без repository source path.
5. Нет одного supported product/operator CLI/service surface.
6. sdist test surface не самовоспроизводим из-за omitted helper files.

Целевой fix: #137.

## 8. Observability / runtime operations / DR

`OperationalReadinessEvaluator` сам по себе качественный deterministic evaluator. Но evaluator не является production control без authoritative state assembler и binding к dispatch.

Подтверждённые разрывы:

- `app/observability` не образует complete telemetry/service layer;
- нет одного authoritative assembler, который собирает actual market-data/stream/broker/OMS/reconciliation/accounting/session/control state;
- нет обязательного final interlock на каждом risk-increasing external mutation;
- нет единой product surface structured logs/metrics/alerts;
- нет завершённого long-running service/process lifecycle;
- backup/restore не квалифицирован как единый product gate;
- `.github/workflows/operational-readiness.yml` на push привязан к historical branch `agent/schema108-paper-core-foundation`, а не canonical `main`.

Целевой fix: #140.

## 9. Bybit canonicalization

Current `main` содержит strategy-free v119-v121 persistence/control slices, а не полный connected Bybit runtime.

Чтобы получить канонический operational product, нужно bounded extraction/integration оставшихся semantics, без wholesale merge historical stack:

- v122 restart-safe session risk;
- one-time session start;
- terminal evidence -> risk commit -> checkpoint ACK;
- persistent active-trade supervisor;
- v123 audited lease recovery;
- read-only connected preflight;
- dedicated trading-key metadata proof;
- fixed-egress/operational-zone binding;
- v124 logical DB identity/bootstrap/readiness;
- exact-symbol approval lineage;
- canonical protected Demo ENTRY path;
- protection/reduce-only recovery.

Но **C2B1 и последующие extraction slices не отменяют product findings F01-F20**. Они могут продолжаться только как честно ограниченная инфраструктурная работа, без заявления, что приложение уже готово.

## 10. End-to-end матрица

| Контур | Статус | Основные причины |
|---|---|---|
| Data acquisition/validation -> signal | FAIL PRODUCT ACCEPTANCE | F14/F16; operational clock/context |
| Signal generation library | COMPONENT PASS | deterministic paper/research implementation |
| Strategy qualification/promotion | FAIL | F15; immutable promotion path отсутствует |
| Strategy profitability | FAIL | frozen negative evidence |
| Risk admission | FAIL | F03/F06/F07/F13 |
| Whole-portfolio valuation | FAIL | F03/F08/F11 |
| OMS durability | STRONG COMPONENT / PRODUCT FAIL | F01/F20 |
| Submit | FAIL | F01/F05/F17/F19 |
| Cancel/replace | FAIL | F02/F07/F18 |
| Stream trust | FAIL | F12; PR #134 repairs only one bounded bypass |
| Execution -> accounting | FAIL | F10/F19 |
| Restart/recovery components | STRONG COMPONENT | integrated convergence/service incomplete |
| Observability/runtime interlock | FAIL | #140 |
| Release/installability | FAIL | #137 |
| Canonical connected Bybit runtime | NOT PRESENT YET | extraction incomplete |
| Real protected Demo ENTRY | NOT PROVEN | no canonical connected chain |
| Connected soak | NOT PROVEN | absent |
| Backup/restore DR | NOT PROVEN AS PRODUCT GATE | incomplete |
| Server governance | FAIL | #103 |
| Live/mainnet | FAIL-CLOSED | correct current safety state |

## 11. PR #134 — оценка F12 fix

PR #134 меняет только:

- `app/application/trade_updates.py`;
- focused provenance tests.

В нём correctly введён processor-level rule: accounting duplicate разрешён только если exact raw-frame digest ранее получил validated stream provenance после parser agreement.

Applicable PR integration workflows: **6/6 SUCCESS**:

- stable-core-quality;
- stable-runtime-import-boundary;
- trading-core-e2e;
- product-composition;
- schema99-external-paper-roundtrip;
- canonical-security-regression.

Canonical-security на PR integration сообщал:

- **298 passed** focused;
- **1141 passed, 2 skipped** full.

Но PR остаётся draft правильно по двум причинам:

1. текущие PR workflows фактически тестируют GitHub synthetic merge `refs/pull/134/merge`, а run metadata показывает branch head; literal exact-head proof нужно фиксировать отдельно, если это обязательный policy gate;
2. PR закрывает **accounting-authority bypass portion of F12**, но не делает весь legacy stream dedup boundary durable/authoritative.

Следовательно #134 полезен и bounded, но не является основанием объявлять broker-event subsystem полностью завершённым.

## 12. Что должно быть изменено, чтобы продукт перестал быть demo/MVP-like composition

### P0-A — факты, identity и accounting

1. Завершить F12 fix.
2. Реализовать #135/#138 durable execution facts + projection convergence.
3. Закрыть F17/F18/F20 immutable economic identities.
4. Immediate fill, stream fill и GET/backfill fill должны сходиться в одну execution truth.

### P0-B — mutation authority и risk reservations

5. Winner-only external mutation claims для submit/cancel/replace.
6. Durable pending reservations.
7. Exact `RiskAuthorization` binding.
8. Re-admit risk-increasing replacements.

### P0-C — data/risk/readiness fail-closed

9. #136 mandatory market-data gate + complete operational risk context.
10. #140 authoritative runtime snapshot + final dispatch interlock.
11. Unknown/missing operational state не имеет optimistic defaults.

### P0-D — installable product

12. #137 packaged migrations/resources.
13. One supported CLI/service entrypoint.
14. Clean-wheel install/bootstrap/self-test CI.
15. Exact resource identity в release evidence.

### P1 — semantic/product completeness

16. F04 explicit clock modes.
17. F08 full valuation snapshot.
18. F11 immutable account/session genesis.
19. #139 strategy qualification/promotion identity.
20. Structured telemetry/alerts/runbooks/DR.
21. #109 physical append-only hardening.

### Canonical Bybit completion

22. Продолжить bounded C2B1/C2B2/C2B3/C2C/C1A/C1B/C3A.
23. Собрать один canonical service path.
24. Выполнить PostgreSQL fault campaign на exact product SHA.
25. Только затем переходить к protected connected Demo evidence.

## 13. Definition of Done

Статус `PRODUCT_READY` разрешён только когда одновременно выполнено:

- все P0 product acceptance properties PASS;
- критические race/crash cases покрыты multiprocess/PostgreSQL tests;
- data quality и operational risk context обязательны и fail-closed;
- pending reservations входят в risk truth;
- broker executions имеют immutable durable source of truth;
- accounting projection restart-safe и convergent;
- exact intent/order/execution identities conflict-aware;
- authoritative runtime readiness/HALT встроены в final dispatch;
- installed artifact самодостаточен;
- canonical Bybit runtime собран в `main`;
- one exact SHA проходит full deterministic/PostgreSQL/security/release product qualification;
- real protected Demo chain PASS;
- connected soak PASS;
- production backup/restore/security/observability PASS;
- positive frozen cost-adjusted OOS strategy evidence существует отдельно;
- server-side governance PASS;
- independent live approval всё ещё остаётся отдельным gate.

До этого момента `live/mainnet` должен оставаться **FAIL_CLOSED**.

## 14. Приоритетная зависимость работ

```text
F12 / F10 / F19 / F20 / F17 / F18
        ↓
F01 / F02 / F03
        ↓
F06 / F07
        ↓
F13 / F14 / F16 + F05/#140
        ↓
F04 / F08 / F09 / F11
        ↓
#137 installable release + #140 service/telemetry/DR
        ↓
remaining bounded Bybit canonicalization
        ↓
exact-SHA PostgreSQL/fault product qualification
        ↓
protected connected Demo chain
        ↓
connected soak
        ↓
separate positive frozen strategy qualification (#139)
        ↓
production governance/security/DR
        ↓
separately approved tiny-capital live pilot
```

Это порядок, который превращает уже сильную инженерную базу в **единый, воспроизводимый и доказуемо работающий продукт**, а не просто увеличивает число функций и тестов.
