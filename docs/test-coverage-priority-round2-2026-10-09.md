# Приоритетное улучшение покрытия — второй этап, 2026-10-09

Продолжение [первого этапа](test-coverage-improvements-2026-10-09.md) от checkout
`76105fd6`. Изменения находятся в рабочем дереве. Приоритет выбран по последствиям:
использование приватных фактов после смены согласия, административные записи,
повторная обработка Telegram update и неверное подтверждение изменения настроек.
Проценты coverage использовались как навигация, а не как критерий полезности теста.

## Контракты и проверяемые последствия

| Граница | Что теперь защищено | Новые passing cases |
| --- | --- | ---: |
| Private memory и lease | Исходный epoch сохраняется между vector, graph и judge; disable→enable и blocked/missing account закрывают старую операцию; cleanup не прерывается повторной отменой | 33 |
| Административная авторизация | Unicode не вызывает HTTP 500; настоящий signed Telegram admin допускается, обычный пользователь, expired/tampered payload и неверный CSRF не вызывают writer | 30 |
| Webhook ingress | Подтверждённый Redis claim сохраняется в ограниченной локальной истории; outage replay не создаёт второй queue item; TTL/capacity/recovery имеют положительные controls | 12 |
| Runtime settings store | Ошибка или cancellation на transaction exit не подтверждает новую revision и не повторяет запись; обычный следующий read сверяет durable outcome | 5 |

Тесты выполняют настоящие публичные функции, Quart routes, HMAC validation,
queue и origin store. Внешние provider/DB/Redis I/O заменяются только на границе.
Для SQL согласия и удаления lease дополнительно используются настоящие PostgreSQL
транзакции и отдельные соединения в disposable clones.

## Исправления перед закреплением тестами

1. `memory.py` ранее терял `expected_epoch` после получения lease и во вложенном
   поиске разрешал новое поколение. Теперь исходное поколение проходит через фазы;
   read gate проверяет epoch, существование account и `private_data_blocked`,
   сохраняя `FOR SHARE OF chat` на транзакционной границе.
2. `memory_consent.py` теперь владеет cleanup task до завершения heartbeat teardown
   и удаления lease, даже при повторной отмене владельца. Cancellation не становится
   успешным результатом; удаляются только свои user/lease identities.
3. `web.py` сравнивает UTF-8 bytes в трёх существующих auth/CSRF/password границах.
   Неверный Unicode input получает обычный отказ; корректные header, password и
   session сценарии, включая синтетический Unicode secret, продолжают работать.
4. `webhook_dedupe.py` записывает полностью подтверждённые update/command claims
   в существующую TTL history. Redis NX остаётся межпроцессной границей; local
   capacity учитывает сразу обе identities и обновление существующей записи.

Production store не менялся: новые тесты закрепляют уже правильное поведение.
Предыдущий тест с названием write failure останавливался на SELECT; новые
пять сценариев доходят до UPDATE и transaction exit.

## Независимое ревью

Все сабагенты использовали `gpt-6.1-sol`: `high` для ограниченных HTTP/cache/ingress
задач и ревью, `xhigh` для consent/cancellation и их независимой проверки.
Границы файлов не пересекались; общие fixtures не менялись.

Ревью авторизации/store и ingress одобрило spec compliance и качество тестов.
Ревью памяти нашло дополнительный Important/P2: graph embedding мог вернуть
`None` после смены epoch, а fallback возвращал ранее найденные факты. Исправлен
также соответствующий exception fallback; шесть новых controls проверяют stale,
blocked и здоровое поколение. Scoped re-review подтвердило устранение замечания.
Предварительный общий unit run был остановлен; его частичные данные не использованы
в итоговом измерении. Финальный общий прогон выполнялся на исправленном snapshot.

Финальный независимый аудит приложения — **APPROVE**: exact nodeid joins,
все setup/call/teardown outcomes, coverage ratios, 11 changed-file hashes,
852 snapshot hashes и неизменность исторических evidence сверены отдельно.

## Итоговая проверка

- Offline unit/E2E: **4522 passed**, без failures/skips;
  516.245 s.
- PostgreSQL/Redis selection: **180 passed**, без failures/skips
  в выбранном наборе; 61.976 s.
- Всего **4702** уникальных passing cases в этих двух прогонах;
  **80 новых** относительно первого этапа.
- Шесть прежних collector E2E не выполнялись: им нужен отдельный isolated stdout
  stack. Первый общий integration run сообщил эти шесть skips; итоговый PG/Redis
  selection явно исключает этот файл. Они не посчитаны как passing или удалённые.
- Locked Ruff, format check (724 Python files), mypy (287 source files), registry
  (111 environment names), UTF-8, links и `git diff --check` прошли.
- SHA256 **852** runtime/config/test files
  совпали до и после общего прогона; оба исторических evidence сохранены без изменений.

| Python metric | Первый этап offline | Второй этап offline |
| --- | ---: | ---: |
| Statement/line coverage | 70.58% | 70.73% |
| Branch coverage | 59.66% | 59.87% |
| Weighted coverage.py total | 68.11% | 68.27% |

| Source | Statement coverage | Branch coverage |
| --- | ---: | ---: |
| `app/repos/memory.py` | 71.06% → 72.27% | 59.46% → 65.13% |
| `app/repos/memory_consent.py` | 47.62% → 64.00% | 38.64% → 54.17% |
| `app/web.py` | 49.48% → 50.17% | 30.33% → 32.67% |
| `app/webhook_dedupe.py` | 80.56% → 90.91% | 68.42% → 81.58% |
| `app/runtime_settings/store.py` | 92.92% → 92.92% | 84.69% → 84.69% |

Эти Python проценты не измеряют SQL/JS/templates. Реальные SQL assertions являются
отдельным свидетельством; current combined coverage здесь не вычислялся.
Фактические nodeids, hashes и результаты сохранены в
[evidence.json](test-coverage/2026-10-09-priority-round2/evidence.json).

## Воспроизведение и границы

Команды и изоляция — в [CONTRIBUTING](../CONTRIBUTING.md). Offline recorder:
[offline_audit.py](test-coverage/2026-10-09/offline_audit.py), mode `unit`, через
`uv run --locked python -X utf8`; отдельный `GEMAIBOT_AUDIT_OUTPUT_DIR`.
Integration: `uv run --locked pytest tests/ -m integration -n 0
--override-ini="addopts=" --timeout=30 --ignore=tests/observability/test_collector_e2e.py`
только с явно isolated test PostgreSQL/Redis. Contract fixtures создают и удаляют
только собственные UUID clones.

Store transaction-exit tests моделируют rollback и потерянное подтверждение
stateful I/O double; они не доказывают timing настоящего PostgreSQL lost ACK.
Локальная dedupe history ограничена TTL/capacity; restart, другая replica при
outage и неизвестный Redis ACK остаются delivery boundaries. Cleanup может ждать
SQL release; при недоступной БД остаётся существующий TTL fallback.
Telegram, провайдеры и VPS не проверялись. Изолированные PG/Redis после проверки
остановлены с проверкой владельцев процессов и портов. Raw RED/GREEN/mutation/
review artifacts сохранены в ignored workspace
`.superpowers/sdd/2026-10-09-priority-coverage-round2/`.

Следующая независимая задача по credential precedence — unreadable encrypted
provider override против отсутствующего override — зафиксирована triage как
более низкий приоритет; поведение этого reader в данном этапе не менялось.
