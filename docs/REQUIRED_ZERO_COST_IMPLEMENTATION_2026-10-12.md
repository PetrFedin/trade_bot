# trade_bot — обязательная программа внедрения с нулевой стоимостью

**Snapshot:** 2026-10-12 · **Status:** REQUIRED / NOT ALL IMPLEMENTED · **Purpose:** persistent work pickup, not release evidence.

**Источник канона:** `README.md`, `CURRENT_SYSTEM_STATUS.json`, PR #297 (merged), PR #296 (closed unmerged), qualification manifests

**Текущее ограничение:** PR #297 объединён; PR #296 закрыт без merge. Стратегическая прибыльность НЕ доказана. Live trading остаётся запрещён независимо от GREEN engineering CI.

## Работы в порядке зависимости

| ID | Priority | Обязательно внедрить | Связи/зависимости | Доказательство завершения |
|---|---|---|---|---|
| AST-ZC-01 | P0 | Пересмотреть безопасный PG restore после закрытого PR #296 | immutable source → create-only isolated target → backup drill → audit | доказано отсутствие записи в source/live; отличающиеся DSN/fingerprint, удалённый тест не нужен если стоит денег |
| AST-ZC-02 | P0 | Заставить qualification отклонять zero-trade/сомнительную историю | historical data → fills → OMS/reconciliation → strategy gate | никаких promotions при 0 сделок, нет look-ahead/survivorship leakage, exact hashes/input snapshots |
| AST-ZC-03 | P0 | Постоянно измерять реальную экономику истории | 21 symbols/6 major → fees/slippage → net PnL → turnover/exit attribution → holdout/walk-forward | reproducible historical windows, full adverse-case metrics и контроль против benchmark; только после анализа менять exit policies |
| AST-ZC-04 | P1 | Добрать server-side protections и evidence | branch protection → immutable logs → readiness authority → operations | независимые checks exact SHA; audit trail и fail-closed order routing |

## Непереговорные ограничения: 0 ₽ новых расходов
- Запрещено создавать/включать платные ресурсы, pay-as-you-go, автоапгрейд, платный AI/API, платную БД, облачное хранилище, CI minutes сверх бесплатной квоты или оплачиваемые production-транзакции.
- Сначала доказывать, что существующая локальная инфраструктура, open-source инструменты или **подтверждённый доступный** бесплатный лимит действительно не требуют оплаты. Если 0-cost ресурс не подтверждён — статус `BLOCKED_NO_FREE_CAPACITY`, не выдумывать альтернативную готовность.
- Не менять и не подключать базу, секреты, деплой другого продукта; никогда не публиковать DSN/токены. Никаких несанкционированных изменений production/main. Кодовые задачи — отдельные PR и существующие quality gates.
- Не запускать реальные платежи/торговлю/рассылки/платные провайдеры ради теста: тесты, моки, sandbox без биллинга или проверенная бесплатная среда.

## Правило для исполнителя / агента
1. Сверить **текущие** main/PR/issues/CI с источниками выше: статусы здесь — snapshot на 2026-10-12, не подтверждение release.
2. Работать по очереди P0 → P1 → P2, но сначала разблокировать зависимости; не начинать следующую бизнес-цепочку, если предыдущая не доказана.
3. Для каждого пункта в PR приложить: exact SHA, файлы, миграции/API/UI связи, локальные тесты, негативные проверки прав, browser/device screenshots (если UI), DB/backup proof (если persistence), live verification (если публичный сервис), стоимость **0 ₽** с источником/лимитом.
4. Отмечать отдельно `PLANNED`, `IN_CODE`, `CI_PASS`, `LIVE_VERIFIED`, `BLOCKED`. Никогда не отмечать `DONE` только из-за документации или зелёного unit-test.
5. Сверить с существующими canonical specs/AGENTS.md; этот чеклист добавляет приоритеты и cost gate, но не заменяет доменную архитектуру/политику.

## Передача следующему проходу
- Выбирай **первый незакрытый незаблокированный** пункт P0, проверяй реальное состояние и открывай точечную реализацию, а не создавай ещё один «план без кода».
- После каждого рабочего прохода обновляй issue/PR и данный чеклист с ссылками на evidence, но не перезаписывай наблюдения устаревшим snapshot.
