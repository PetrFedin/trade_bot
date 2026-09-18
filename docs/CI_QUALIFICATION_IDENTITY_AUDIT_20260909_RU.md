# Аудит идентичности CI-квалификации

Дата: 2026-09-09

## Вывод

Текущие pull-request workflows дают полезное и сильное доказательство совместимости точной пары `base + head`, но по факту тестируют автоматически созданный GitHub merge commit `refs/pull/<PR>/merge`, а не обязательно буквальный commit head-ветки.

Это не делает тесты недействительными. Ошибка возникает только тогда, когда такое доказательство называется «literal exact-head checkout PASS».

## Подтверждённый пример — PR #134

- base: `ff684ab85b92151c215c7d5cc85bfc527fecb5eb`;
- branch head: `35a39c621e1956eec06916b6805b485ce83d695c`;
- synthetic merge: `bd5800aa450ca61ab0f5f02a747a721c0bce1ab8`;
- PR run metadata указывает head `35a39c...`;
- checkout в canonical-security фактически переключился на `refs/pull/134/merge` / `bd5800aa...`.

Для этой пары все шесть применимых PR-workflow завершились SUCCESS, включая PostgreSQL canonical-security. Корректная формулировка результата:

> exact-base+head PR integration PASS on GitHub synthetic merge.

Некорректная формулировка:

> literal branch exact-head checkout PASS.

## Почему это важно

В проекте release/process документы требуют точной фиксации SHA. Если не разделять:

1. candidate branch head;
2. tested synthetic merge;
3. post-merge canonical main;

можно непреднамеренно собрать цепочку доказательств из разных физических ревизий и назвать её одним exact-head proof.

## Требуемый контракт

Каждый qualification artifact должен содержать минимум:

- `candidate_head_sha`;
- `candidate_base_sha`;
- `tested_checkout_sha`;
- `tested_checkout_kind = BRANCH_HEAD | PR_SYNTHETIC_MERGE | CANONICAL_MAIN | TAG`;
- workflow/run/job identity;
- Git tree SHA при release-critical gate;
- список применимых gates и их outcome.

Если политика требует буквального branch-head proof до merge, workflow должен явно checkout:

```text
${{ github.event.pull_request.head.sha }}
```

или использовать отдельный workflow_dispatch/commit qualification, жёстко привязанный к указанному SHA.

После merge всё равно требуется отдельная квалификация фактического `main` SHA для тех утверждений, которые относятся к каноническому продукту.

## Что не нужно делать

- не отключать synthetic merge testing: оно полезно для обнаружения конфликтов с base;
- не заменять integration test только head-only test;
- не считать metadata `head_sha` доказательством того, что именно этот commit был checked out;
- не переименовывать прежние green runs в FAIL: их следует переклассифицировать как integration evidence соответствующей пары base+head.

## Статус

`CIQ01 = OPEN / P1`.

Он не является непосредственной торговой уязвимостью, но влияет на достоверность release evidence и поэтому должен быть закрыт до production qualification.
