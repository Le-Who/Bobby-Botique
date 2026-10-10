# Приоритетное покрытие тестами — второй этап

**Goal:** закрыть наиболее существенные оставшиеся пробелы в контрактах приватной памяти, административного доступа, Telegram ingress и сохранения настроек.

**Architecture:** проверять существующие публичные границы и наблюдаемые последствия. Подменять только внешние I/O, сохраняя настоящие функции проверки доступа, маршруты, поиск и cache. Исправлять подтверждённые дефекты после RED; не менять продуктовую политику.

**Tech Stack:** Python 3.14, uv 0.12.6, pytest, Quart, asyncio; существующий offline recorder.

**Исходная точка:** `76105fd6`; [предыдущий отчёт](../../test-coverage-improvements-2026-10-09.md). Предыдущие измерения сохраняются отдельно.

## Ограничения

- Не читать `.env`, не обращаться к Telegram, провайдерам или production-сервисам. Только синтетические данные.
- Не менять зависимости, модельные defaults, SQL-схему и публичные интерфейсы без доказанной необходимости.
- Модель всех сабагентов — `gpt-6.1-sol`; reasoning не ниже `high`. Для consent/cancellation — `xhigh`.
- Независимые задания имеют непересекающиеся файлы; общие fixtures и предыдущие evidence не редактируются.
- Коммиты, push и deployment не входят в этот этап. Прогон integration нельзя объявлять успешным без реальных изолированных сервисов.

## Review focus

Проверить disable→enable между фазами чтения памяти; повторную отмену во время cleanup; подписанного обычного пользователя в административном маршруте; Unicode в заголовке/форме; Redis success→outage→recovery и неизвестный результат COMMIT. Каждый отказ должен проверять отсутствие запрещённого чтения, provider call, queue item или записи. Положительные сценарии не должны ломаться.

### Task 1: Точное поколение согласия и cleanup lease

**Files:** `app/repos/memory.py`, `app/repos/memory_consent.py`, новые `tests/test_memory_retrieval_epoch.py`, `tests/test_private_lease_cleanup.py`, `tests/integration/contracts/test_memory_retrieval_epoch_postgres.py`; затронутые существующие тесты — только при необходимости совместимости.

- [x] Воспроизвести смену поколения после получения lease во время embedding/expansion/candidate retrieval. Настоящие публичные search, graph и judge должны вернуть пустой результат и не использовать данные нового поколения для старой операции.
- [x] Сохранить пустой результат при смене поколения также в fallback после `None`/ошибки graph embedding; положительный fallback при неизменном поколении остаётся полезным.
- [x] Передавать исходный `expected_epoch` во вложенные фазы, проверять его на транзакционной границе чтения и перед отправкой приватных фактов judge. Сохранить API и положительный результат при неизменном поколении.
- [x] Через asyncio Events доказать, что вторая отмена во время остановки heartbeat или release не завершает владельца до cleanup; cancellation остаётся cancellation, heartbeat и release завершаются.
- [x] Настоящими PostgreSQL-транзакциями проверить старое поколение после disable→enable и положительный поиск при неизменном поколении. Использовать только disposable contract clones.
- [x] Запустить новые тесты и соседние memory/lease/graph/runtime tests; сохранить RED/GREEN evidence.

### Task 2: Административная авторизация на настоящих HTTP-маршрутах

**Files:** `app/web.py`, новый `tests/test_admin_auth_contracts.py`.

- [x] Воспроизвести Unicode `X-Auth-Token`, password и CSRF через Quart client. Неверные входные данные должны отказать в доступе без HTTP 500 и без записи.
- [x] Исправить сравнение только в существующих auth-границах; сохранить корректные header/password/session сценарии.
- [x] Реально подписать Telegram initData синтетическим токеном: текущий admin разрешён, обычный пользователь, просроченные и повреждённые данные запрещены. Проверить writer side effects настоящего controls-маршрута.
- [x] Проверить соседние web security и controls tests; сохранить RED/GREEN evidence.

### Task 3: Дедупликация при отказе Redis после успешного приёма

**Files:** `app/webhook_dedupe.py`, `tests/test_webhook_dedupe.py`, `tests/e2e/test_webhook_lifecycle.py`.

- [x] Воспроизвести повтор update и command identity после подтверждённого Redis claim и перехода в local fallback. Через настоящий webhook доказать единственный queue item.
- [x] Сохранить ограниченную локальную историю подтверждённых claims для fallback; проверить expiry, capacity, новые updates и восстановление Redis. Redis остаётся межпроцессной границей.
- [x] Проверить соседние webhook/backpressure tests. Не обещать exactly-once при потере/неизвестном подтверждении Redis или рестарте.

### Task 4: Ошибка COMMIT при изменении runtime-настроек

**Files:** `tests/test_runtime_settings_store.py`; production store — только если регрессия подтвердит дефект.

- [x] Использовать stateful transaction double на I/O-границе: UPDATE возвращает rows, затем transaction exit падает до commit либо после persist с потерянным подтверждением.
- [x] Ошибка передаётся вызывающему коду, unconfirmed revision не объявляется принятой, автоматический повтор записи отсутствует. Следующий обычный read перечитывает durable outcome вместо старого cache.
- [x] Покрыть cancellation и успешную запись; доказать чувствительность к реалистичной ошибке cache invalidation без изменения принятой политики.

### Task 5: Независимая проверка и общий прогон

- [x] Независимый reviewer проверяет spec compliance, объективность assertions, RED/GREEN evidence и cleanup. Исправить существенные замечания.
- [x] Запустить полный offline unit/E2E набор с coverage, locked Ruff/format/mypy, encoding, links и diff checks.
- [x] Сохранить отдельный итоговый отчёт с точными результатами, новыми сценариями и ограничениями; не переписывать исторические измерения.
