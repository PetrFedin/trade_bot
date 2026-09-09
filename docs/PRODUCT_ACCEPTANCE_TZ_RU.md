# ТЗ — критерии приёмки ASTRA / trade_bot как полноценного продукта

Дата фиксации: 2026-09-09  
Базовый SHA аудита: `ff684ab85b92151c215c7d5cc85bfc527fecb5eb`

## 1. Цель

Цель проекта — не наличие большого количества модулей, тестов или исследовательских веток, а единый воспроизводимый торговый продукт, который безопасно проходит полный жизненный цикл от данных до закрытого и сверенного результата.

Продукт считается готовым только тогда, когда одна каноническая версия кода и одна каноническая конфигурация доказывают сквозную цепочку:

```text
источники данных
-> валидация и актуальность
-> сигнал
-> план сделки
-> портфельный и сессионный риск
-> резервирование риска/денег
-> авторизация
-> OMS
-> единственная внешняя мутация
-> биржевое подтверждение/неопределённость
-> исполнение/частичное исполнение
-> защита позиции
-> сопровождение
-> закрытие
-> комиссии/фандинг/PnL
-> бухгалтерская проекция
-> сверка с брокером
-> терминальное доказательство
-> восстановление после рестарта
-> готовность к следующей операции
```

Ни один этап не может считаться пройденным на основании факта наличия класса, файла, теста или исторически зелёного PR.

## 2. Принципы приёмки

1. **Один канонический продукт.** Исторические stacked PR и research-ветки являются источниками семантики и доказательств, но не отдельными частями работающего релиза.
2. **Один exact SHA на доказательство.** Нельзя собирать release PASS из зелёных результатов разных несовместимых SHA.
3. **Fail-closed.** Неизвестное состояние, устаревшие данные, несовпадение брокера и локального состояния, потерянная авторизация или неясный результат мутации запрещают новый риск.
4. **Факт брокера выше внутренней модели.** Реальное исполнение нельзя удалить или игнорировать из-за ошибки внутреннего учёта; оно должно быть сохранено и доведено до сверенного состояния.
5. **At-most-once относится к внешней мутации, а не только к записи события.** Идемпотентное состояние без эксклюзивного права на POST недостаточно.
6. **Никаких слепых retry мутаций.** Неопределённый submit/amend/cancel сначала разрешается чтением брокерской истины.
7. **Risk-increasing amendment = новая оценка риска.** Любая замена, увеличивающая цену/количество/нотионал/маржинальный риск, проходит повторный admission.
8. **Pending риск учитывается.** Открытые/ожидающие заявки резервируют деньги и портфельный лимит до фактического освобождения.
9. **Demo не равно production.** Зелёный тестовый стенд, Paper и Demo не доказывают mainnet.
10. **Engineering readiness не равно profitability.** Без положительного замороженного cost-adjusted OOS edge стратегия не продвигается.

## 3. Каноническая архитектура продукта

### 3.1 Данные и время

Обязательные компоненты:
- биржевое/server time и локальный monotonic/wall clock;
- явное разделение `REPLAY_CLOCK` и `OPERATIONAL_CLOCK`;
- timestamp источника, получения, решения и исполнения;
- freshness SLA для каждого класса данных;
- gap/duplicate/out-of-order detection;
- point-in-time правило: решение использует только информацию, доступную на момент решения;
- явный статус недоступности источника без подстановки нулей/будущих значений.

Приёмка:
- старые данные не могут стать свежими только потому, что `decision_time` взят из их собственного timestamp;
- clock skew/rate-limit/outage/reconnect тестируются как E2E fault cases;
- held/reserved portfolio valuation получает свежие цены для всех необходимых символов.

### 3.2 Стратегия и исследовательский контур

Обязательные компоненты:
- неизменяемая идентичность стратегии, параметров, universe, данных и cost model;
- отсутствие look-ahead;
- комиссии, spread, slippage, funding и реалистичная очередность intrabar;
- train/validation/OOS/holdout с замороженными границами;
- regime/sample/concentration/sensitivity анализ;
- MFE/MAE, drawdown, tail, turnover, profit factor, expectancy;
- запрет автоматического self-promotion из retrospective результата.

Приёмка стратегии:
- положительный net expectancy на untouched/OOS;
- достаточная выборка по символам, времени и режимам;
- приемлемый drawdown/tail;
- устойчивость к разумным изменениям cost/parameter assumptions;
- отсутствие результата, зависящего от одного символа/эпизода;
- отдельный frozen promotion artifact.

Текущее состояние: **FAIL / `PROFITABILITY_NOT_PROVEN`**. Отрицательный frozen Bybit price-only результат сохраняется как доказательство и не переписывается.

### 3.3 Портфель и риск

Обязательные состояния экспозиции:
- broker position;
- acknowledged/open orders;
- pending/outbox orders;
- reservations;
- fills not yet fully projected into accounting;
- recovery-only exposure.

Risk admission обязан учитывать:
- cash/equity;
- текущие позиции;
- reserved cash/notional;
- gross/net exposure;
- symbol/side concentration;
- session/daily loss and drawdown;
- concurrent positions;
- liquidation/margin constraints для derivatives;
- состояние HALT/KILL/READ_ONLY.

Приёмка:
- невозможно поставить несколько заявок, каждая из которых отдельно проходит лимит, но совместно его превышают;
- replace с ростом риска проходит повторную оценку;
- риск-решение связано с exact intent и canonical economics;
- рестарт не сбрасывает opening equity/high-water/loss history.

### 3.4 OMS и внешние мутации

Для каждого risk-increasing действия должен существовать durable lifecycle:

```text
INTENT
-> RISK_APPROVED
-> RESERVED
-> OUTBOXED
-> EXCLUSIVE_CLAIM
-> SUBMIT_STARTED
-> BROKER_RESULT | UNCERTAIN
-> RECONCILING
-> ACK/FILL/CANCEL/REJECT
-> TERMINAL
```

Ключевой контракт: только владелец `EXCLUSIVE_CLAIM` имеет право вызвать внешний POST. Второй worker обязан перейти к чтению/сверке и не получает mutation capability.

Требования:
- SQLite и PostgreSQL CAS/transaction semantics;
- multiprocess, а не только multithread tests;
- crash before/after claim, before/after POST, before/after response;
- no blind retry;
- mutation cancel/replace имеет тот же claim contract;
- outbox claim имеет owner/generation/fencing semantics или эквивалентную безопасную модель.

### 3.5 Broker event / WebSocket / fill trust

Внешний execution event сначала проходит:
- authenticated/listening stream state;
- schema validation;
- symbol/order identity validation;
- monotonic cumulative fill/time validation;
- dedup provenance;
- durable raw execution inbox.

Только после этого событие может изменять OMS/portfolio/accounting.

Нельзя помещать digest непроверенного события в trusted dedup set так, чтобы повторное получение обходило исходную проверку.

### 3.6 Учёт и финансовая проекция

Нужно отделить:
1. **неизменяемый факт брокера** — execution/fill/fee/funding/closed PnL;
2. **внутреннюю финансовую проекцию** — cash, position, realized/unrealized, session risk;
3. **состояние сверки**.

Если проекция не применима из-за внутреннего инварианта, факт брокера остаётся durable, а система переходит в `ACCOUNTING_PENDING/QUARANTINED`, блокируя новый риск до восстановления.

Требования:
- durable execution inbox;
- idempotent projection checkpoint;
- immutable account/session genesis;
- cash-flow events вместо передачи произвольного `opening_cash` при каждом replay;
- комиссии/funding не могут создавать нерепродуцируемый state split;
- OMS, execution inbox, accounting projection и broker reconciliation сходятся после crash/retry.

### 3.7 Bybit Demo operational plane

Канонический продукт должен включить, без wholesale merge исторических веток:
- v119 runtime lease + active checkpoint;
- v120 approval/provenance/terminal evidence;
- v121 ARM/HALT;
- v122 session risk;
- one-time session start;
- terminal evidence -> risk commit -> checkpoint ACK;
- persistent supervisor;
- v123 audited lease recovery;
- connected read-only preflight;
- dedicated trading-key metadata preflight;
- protected fixed egress;
- same-account proof;
- exact operational-zone binding;
- v124 logical DB identity;
- exact-symbol short-lived operator approval;
- one canonical Demo ENTRY path;
- verified protection / reduce-only recovery;
- strict release evidence chain.

До канонизации всех зависимостей и устранения P0 acceptance gaps запуск нового Demo ENTRY запрещён.

### 3.8 Рабочее приложение и эксплуатация

Полноценный продукт должен иметь один официальный способ запуска, а не набор тестовых builders:
- CLI/service entrypoint;
- production config schema;
- schema verify/bootstrap command;
- long-running supervisor/service;
- health/readiness/liveness endpoints или эквивалентные operational surfaces;
- graceful shutdown;
- idempotent restart;
- structured logging с correlation ids;
- metrics: data age, queue depth, reservations, broker latency/error rate, reconnects, unresolved mutations, reconciliation mismatch, positions, PnL, drawdown, protection state;
- alerts с owner/runbook;
- deployment definition (container/Kubernetes/system service as chosen);
- rollback;
- backup/restore.

Readiness evaluator, не связанный с реальным dispatch gate и alerting, сам по себе не удовлетворяет этому разделу.

### 3.9 Release package

Wheel/sdist/container должны содержать всё необходимое для запуска exact release:
- Python code;
- все используемые SQL migrations;
- required static assets;
- config schema/default-safe templates;
- version/commit identity;
- SBOM/provenance;
- bootstrap/readiness tooling.

Приёмка проводится из **пустой среды** только по release artifact, а не по checkout репозитория.

Минимальный release drill:
1. установить artifact;
2. проверить identity/digest;
3. bootstrap/verify пустой PostgreSQL;
4. запустить offline/self-test;
5. запустить integration suite;
6. rollback/restore test;
7. доказать отсутствие ссылок на файлы, которые существуют только в Git checkout.

### 3.10 Security и governance

Обязательные условия:
- protected `main`;
- PR required;
- required checks;
- stale approval dismissal;
- CODEOWNERS/review ownership для execution/risk/migrations;
- force-push/deletion disabled;
- production environment protected;
- runtime DB roles non-owner/least privilege;
- no UPDATE/DELETE/TRUNCATE bypass для append-only evidence;
- workload identity/KMS/HSM или эквивалент для production secrets;
- rotation/revocation drill;
- dependency/action pinning и audit.

### 3.11 Reliability / fault campaign

Обязательная матрица отказов:
- process kill в каждой mutation phase;
- два/несколько workers;
- DB disconnect/restart;
- WebSocket disconnect/out-of-order/duplicate;
- REST timeout/429/5xx;
- broker success + lost response;
- partial fill;
- cancel/replace collision;
- stale quote;
- clock skew;
- protection missing/drift;
- broker/local mismatch;
- hard kill с orphan lease;
- backup restore/failover.

Hard acceptance:
- zero unintended duplicate ENTRY;
- zero silent exposure increase after unknown state;
- zero unresolved orphan exposure at release gate;
- every uncertain mutation converges through reads/reconciliation;
- recovery cannot create replacement entry.

### 3.12 Connected Demo soak

После первой успешной protected Demo chain нужен продолжительный connected soak. Минимум должен покрыть реальные циклы:
- data refresh;
- no-signal periods;
- signal/blocked signal;
- approved trade;
- partial/normal close where exchange naturally produces them;
- restart;
- reconnect;
- HALT and recovery drill;
- DB maintenance/restore drill.

В течение soak должны собираться SLO и доказательства отсутствия дубликатов/несверенных позиций.

### 3.13 Live gate

Mainnet ENTRY остаётся запрещённым, пока отдельно не PASS:
- product acceptance;
- positive frozen strategy evidence;
- connected Demo chain;
- soak/fault recovery;
- governance;
- production security/DR;
- observability/alerts;
- independent live approver.

Первый mainnet шаг — отдельный tiny-capital pilot с жёсткими notional/drawdown limits и без автоматического масштабирования.

## 4. Обязательная E2E-матрица приёмки

| Сценарий | Ожидаемый результат |
|---|---|
| Нормальная сделка | один submit, сверенное исполнение, защита, terminal accounting, restart parity |
| Два worker на одном submit | один внешний POST максимум |
| Два worker cancel/replace | одна внешняя мутация максимум |
| Потерян ответ после broker success | только GET/reconcile, без повторного POST |
| 3 pending BUY при лимите на 1 | второй/последующие блокируются резервами |
| Replace увеличивает риск | новый risk admission обязателен |
| HALT после approval, до submit | broker submit не выполняется |
| Старая market data | operational entry блокируется |
| Удерживается другой symbol | полная свежая portfolio valuation доступна |
| Unauthenticated WS fill | не влияет на OMS/accounting |
| Duplicate valid fill | ровно один экономический эффект |
| Accounting projection failure | execution fact сохраняется, новый риск блокируется, recovery детерминирован |
| Restart с тем же history | тот же genesis/equity/positions/revision |
| Restart с другим opening cash | запрещён без explicit cash-flow/genesis change |
| DB/WS/REST crash points | state converges fail-closed |
| Clean install from wheel/sdist | migrations/bootstrap/service полностью доступны |
| Bybit Demo exact-head chain | INFRA->SESSION->SUPERVISOR->ARM->ENTRY->HALT->RECOVERY на одном SHA |

## 5. Definition of Done продукта

`PRODUCT_READY = PASS` только если одновременно:

- все P0 acceptance tests зелёные на SQLite и PostgreSQL, необходимые race cases — multiprocess;
- все P1 functional gaps закрыты или явно не входят в утверждённый product scope;
- канонический Bybit runtime собран в `main`, а не в историческом stacked PR;
- release artifact самодостаточен;
- один exact SHA проходит full CI, security, supply-chain и release provenance;
- real protected Demo connected chain PASS;
- connected soak PASS;
- стратегия имеет отдельный положительный frozen cost-adjusted OOS PASS;
- branch protection и production governance PASS;
- backup/restore, observability, alerting и runbooks PASS;
- live остаётся отдельным gate.

До выполнения этих условий корректный статус проекта: **сильная инженерная платформа с существенными квалифицированными компонентами, но не завершённый production trading product**.
