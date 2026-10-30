# Request Runtime and Queue Reliability Implementation Plan

> **Implementation record:** See the [dated implementation evidence](../../request-processing-dashboard-implementation-2026-10-28.md) and current runtime-controls guide. The original checklist below is retained as design history; its unchecked boxes do not establish current runtime behavior.

> **For agentic workers:** Use `executing-plans` task by task; use `subagent-driven-development` only for explicitly delegated execution. Checkboxes describe future work, not completed changes.

**Goal:** Ограничить всю принятую работу, сделать queue outcomes и retries правдивыми, исключить воспроизведённый дубль Redis/local fallback.

**Architecture:** Сохранить публичный фасад `app.queue`, разделив типы результата, хранение статуса и расписание. Ingress capacity принадлежит очереди PTB до `task_done`; lease и cancel принадлежат владельцу выполняемой операции. Provider router и delivery остаются текущими владельцами генерации и пользовательского вывода.

**Tech Stack:** Locked Python 3.14, asyncio, python-telegram-bot, Redis/Lua, pytest; без нового брокера.

**Spec:** [Аудит](../../request-processing-dashboard-audit-2026-10-28.md), разделы 3–4; [общие ограничения и gates](2026-10-28-request-processing-and-dashboards.md).

## Global Constraints

- Все ограничения master plan применяются. Не использовать production Redis для тестов.
- Per-user lock остаётся перед глобальным update semaphore; inline сохраняет свой обход пользовательского lock.
- Не заменять domain failure на exception, если это создаёт автоматический повтор уже выполненной доставки.
- Owner leases и сравнение точных serialized payload в ACK/NACK/recovery сохраняются до явного versioned cutover.
- Локальный fallback и Redis режимы имеют одинаковые лимиты/семантику, но разные гарантии восстановления; UI должен это различать.

## Review Focus

- PTB sentinel, ошибка processor и отмена до запуска: admission освобождён — R1.
- Renewal ответил после потери token: прежний token не оживает — R2.
- Возвращённый failed outcome, partial и unknown delivery: разные terminal/retry transitions — R3.
- Enqueue завершился на сервере без ответа клиенту: не возникает второй execution — R4a.
- Restart при running job, cancel и exhausted budget: stale owner не ACK/commit, recovery конечен — R4b/R4c.

## Карта файлов и контрактов

| Файл | Назначение |
| --- | --- |
| Новый `app/update_queue.py` | Очередь с bounded accepted outstanding, совместимая с публичным queue API PTB |
| `app/adapters/concurrency.py` | Общий admission deadline, подтверждение lease и завершение владельца |
| Новый `app/queue_contracts.py` | Typed result/admission/status/scheduling policy; без Redis/Telegram импорта |
| Новый `app/queue_store.py` | Атомарная admission, shared metadata, transitions с owner fencing |
| Новый `app/queue_scheduling.py` | Delayed/expiry/backoff policy; test clock/random injection |
| `app/queue.py` | Facade, worker lifecycle, handler dispatch и совместимость текущих callers |
| `app/deferred_response.py` | Явный business outcome и безопасная retry eligibility |

### R1. Ограничить outstanding updates на входе

**Finding:** R01. **Файлы:** создать `app/update_queue.py`, `tests/test_update_queue_capacity.py`; изменить `bot.py`, `tests/test_webhook_admission.py`, `tests/test_update_processor.py`; проверить `tests/test_webhook_dedupe.py`, `tests/e2e/test_webhook_admission_cancellation.py`.

**Интерфейс:** `OutstandingUpdateQueue(capacity: int)` с поддерживаемыми PTB `put/put_nowait/get/get_nowait/task_done/join/full/qsize`; `outstanding` включает queued + fetched but unfinished. `buffered` и `outstanding` — разные метрики. Начальный capacity равен текущему вычисленному `UPDATE_QUEUE_MAXSIZE` с существующей нижней границей. Это не новый скрытый множитель.

- [ ] Добавить regression на настоящий fetcher установленного PTB: capacity=100, 1000 updates, заблокированный handler одного пользователя. Утверждения: `outstanding <= 100`, buffered может быть 0, следующий `put_nowait` выдаёт `QueueFull`, pending producers не превращаются в 900 processing wrappers.
- [ ] Добавить lifecycle случаи: completed, raised, cancelled before processor, shutdown sentinel и drain. После завершения `outstanding == 0`, `join` завершается; повторный `task_done` обнаруживает нарушение. Ожидание `put` при polling даёт backpressure без молчаливой потери update.
- [ ] Реализовать очередь через собственный учёт outstanding и публичный PTB queue contract; не менять библиотеку/её private fetcher. Ресурс брать при успешном put, освобождать при terminal `task_done`, а не при get. Проверить совместимость sentinel, чтобы shutdown не зависал на заполненной очереди.
- [ ] В webhook сохранить существующий порядок admission/dedupe и ownership release: capacity reject возвращает текущий retryable HTTP ответ; update не остаётся ошибочно deduped. Отмена между claim и put освобождает только принадлежащий запросу claim.
- [ ] Сохранить тест справедливости: другой уже принятый пользователь и inline проходят, пока hot user ждёт свой lock. Полный ingress вправе отклонять новые updates; отдельную reserved lane не вводить без нового требования.
- [ ] Проверить и задокументировать новые значения outstanding/buffered:

```powershell
uv run --locked pytest tests/test_update_queue_capacity.py tests/test_update_processor.py tests/test_webhook_admission.py tests/test_webhook_dedupe.py tests/e2e/test_webhook_admission_cancellation.py --override-ini="addopts=" --timeout=30 -q
```

**Готово, когда:** буфер и созданные PTB wrappers вместе имеют один предел; fairness и retry webhook не ухудшены.

### R2. Один admission deadline и подтверждённое владение semaphore

**Findings:** R02/R03. **Файлы:** `app/adapters/concurrency.py`, `tests/test_concurrency.py`, `tests/test_concurrency_hardening.py`; создать `tests/integration/redis_contracts/test_semaphore_lease_ownership.py`.

**Интерфейс:** существующий `GlobalLLMSemaphore` сохраняется; private helper остатка deadline использует monotonic clock. Ввести явную внутреннюю причину `lease_lost`; owner task и lease confirmation хранятся в request context. Public failure продолжает проходить через действующий error/delivery owner.

- [ ] Зафиксировать тест: local wait=119 s при timeout=120 s оставляет Redis-фазе около 1 s; суммарное ожидание не становится 239 s. Fake clock не должен реально ждать минуты.
- [ ] Один раз вычислить `deadline = monotonic() + timeout`; local acquire, Redis eval/retries и sleeps используют остаток. Сетевые socket timeout сохранить; собственный `asyncio.timeout(remaining)` ограничивает весь admission. При истечении вернуть текущую overload-ошибку и освободить local/Redis token.
- [ ] Написать Redis Lua contract tests: существующий member продлён → success; отсутствующий → lost; позднее продление после release не создаёт member. Заменить прямой `ZADD XX` на атомарные exists + update с однозначным return code.
- [ ] Разделить lease TTL и admission timeout как внутренние значения, даже если стартовые defaults одинаковы. После успешного acquisition отслеживать последнюю подтверждённую lease. При доказанной потере — отменить принадлежащую scope работу; при ошибке renewal — повторять только до безопасной границы последнего подтверждения, затем завершить scope с `lease_lost`. Не переводить уже потерянного distributed owner в local fallback посреди работы.
- [ ] Ограничить release I/O отдельным cleanup deadline 5 s; обязательную локальную уборку выполнять в `finally`. Не заменять исходную CancelledError и не оставлять бесконечно живой cleanup child. Истёкший Redis token удалится TTL/следующим admission; поздняя уборка не удаляет token нового владельца.
- [ ] Проверить двух владельцев при limit=1: после потери token старый provider coroutine отменён и joined, до завершения его безопасной остановки не заявлять успешное владение старым. Сохранить тесты начального `local_fallback`; метрики показывают degraded scope.

```powershell
uv run --locked pytest tests/test_concurrency.py tests/test_concurrency_hardening.py --override-ini="addopts=" --timeout=30 -q
```

Redis contract выполнить отдельно по integration recipe. Lease не может отменить уже совершившийся внешний side effect; это ограничение описать. Для сроков asyncio ориентироваться на [официальную семантику timeout/cancellation](https://docs.python.org/3.14/library/asyncio-task.html#asyncio.timeout), для владения lease — на [паттерны Redis](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/).

### R3. Типизированные исходы, конечный retry budget и local FIFO

**Findings:** Q01/Q03/Q06. **Файлы:** создать `app/queue_contracts.py`, `tests/test_queue_outcomes.py`; изменить `app/queue.py`, `app/deferred_response.py`, `tests/test_task_queue.py`, `tests/test_redis_queue.py`, `tests/test_queue_retry_capacity.py`.

**Предлагаемый типовой контракт:**

```python
class JobOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    UNKNOWN_DELIVERY = "unknown_delivery"

@dataclass(frozen=True)
class JobResult:
    outcome: JobOutcome
    reason_code: str | None = None
    result_summary: Mapping[str, int | str | bool] = field(default_factory=dict)
```

`reason_code` — allowlist, не `str(provider_exception)`. `result_summary` не содержит prompt/history/raw response. Поля status-store описаны R4b.

- [ ] Регрессия actual worker + deferred handler: returned failure не увеличивает completed, terminal event согласован со status, retryable назначает следующую попытку только при оставшемся бюджете. Отдельно partial delivered → succeeded с summary `partial=true`; unknown delivery → terminal unknown без автоматической отправки.
- [ ] Явно перевести три зарегистрированных handler типа (deferred, document processing, metrics cleanup) на `JobResult`; unknown task type — permanent failure. Ошибки валидации permanent; временный отказ до side effect retryable; после неоднозначной отправки unknown. Не интерпретировать любой dict как успех.
- [ ] Worker сначала вычисляет transition по outcome, затем выполняет ACK/NACK и обновляет counters/events согласно одной модели. Ошибка ACK не делает внешний side effect неслучившимся: сохранить unknown/reconciliation путь.
- [ ] Унифицировать бюджет: `max_attempts=3` по умолчанию означает не более трёх начавшихся executions, включая прерванные. Считать попытку при claim/start. Recovery не начинает четвёртую; повторный recovery одного owner не считает попытку снова. Старое поле `max_retries` нормализовать один раз при чтении старого payload и зафиксировать migration semantics тестом.
- [ ] Local priority key сделать `(-priority, enqueue_sequence, task_id)`; sequence монотонный в процессе, сериализованное submitted_at используется для age, не UUID. Проверить FIFO 50 равноприоритетных jobs и сохранение различий приоритетов.
- [ ] Прогнать focused suite; затем проверить согласованность `job.finished`, queue stats и operational metrics, обновив их owner contract.

```powershell
uv run --locked pytest tests/test_queue_outcomes.py tests/test_task_queue.py tests/test_redis_queue.py tests/test_queue_retry_capacity.py tests/observability/test_operational_metrics.py --override-ini="addopts=" --timeout=30 -q
```

### R4a. Идемпотентная admission без двойного Redis/local enqueue

**Finding:** Q02. **Зависимость:** R3. **Файлы:** создать `app/queue_store.py`, `tests/test_queue_admission.py`; изменить `app/queue.py`, `app/deferred_response.py`; дополнить `tests/integration/test_queue_lua.py`.

**Контракт в `queue_contracts.py`:** `AdmissionState = accepted | existing | rejected | unknown`; `QueueAdmission(state, task_id, reason_code, storage_mode)`. Новый `enqueue_job(..., idempotency_key: str, lane: str, policy: QueuePolicy) -> QueueAdmission`; действующий `add_background_task` остаётся адаптером для существующих callers. `unknown` не маскировать под уверенный отказ с предложением создать другую копию.

- [ ] Воспроизвести commit-then-disconnect: сервер принял admission, клиент не получил ответ. Повтор с тем же idempotency key возвращает тот же task_id; handler вызывается один раз; local queue остаётся пустой.
- [ ] В Lua атомарно связать idempotency key, payload reference, начальный status и ready-list entry. Повтор не создаёт дополнительный list entry. Ключ строится из стабильного operation ID/вида действия, не текста запроса и не нового UUID при retry HTTP.
- [ ] После ошибки уже отправленной Redis mutation выполнить bounded lookup по тому же key. Если Redis недоступен — вернуть unknown с task_id, не копировать в память. У caller оставить возможность status reconciliation по идентификатору.
- [ ] Local fallback разрешать только когда Redis backend не был выбран/запись не предпринималась. Отмечать `storage_mode=local`; local admission тоже проверяет idempotency key в пределах retention. Не обещать защиту от потери process memory.
- [ ] Защитить admission от превышения лимитов атомарно; сами policy значения и delayed indexes реализуются R4c. Сделать чтение/serialization versions явными; подготовить drain/cutover, не исполнять его в рамках тестов.
- [ ] Выполнить `tests/test_queue_admission.py`, `tests/test_redis_queue.py` и реальный Lua contract в изолированном Redis. Ожидается отсутствие дубля при потере ответа, timeout, повторном HTTP и restart worker.

### R4b. Общий статус, owner fencing, отмена и retention

**Findings:** Q03/Q05. **Зависимость:** R4a. **Файлы:** `app/queue_contracts.py`, `app/queue_store.py`, `app/queue.py`, `app/repos/users.py`; создать `tests/test_queue_job_lifecycle.py`; расширить `tests/integration/test_queue_lua.py` и существующие account-erasure tests.

**Контракты:**

- `JobState = queued | delayed | running | cancel_requested | succeeded | failed | cancelled | expired | unknown_delivery`.
- `JobSnapshot`: task_id, kind, lane, state, revision, attempts_started, max_attempts, submitted_at, started_at, finished_at, not_before, deadline_at, heartbeat_at, reason_code, storage_mode, retry_eligible, retry_payload_available; публичная версия исключает payload/user data.
- `read_job(task_id: str) -> JobSnapshot | None`; `request_cancel(task_id: str, *, expected_revision: int, actor: str, operation_id: str) -> JobSnapshot`; `claim/finish/requeue` требуют owner token. Deadline/time для Redis state берётся из server time, не часов replica.

- [ ] Тест двумя Queue instances: producer видит actual running/terminal worker; отмена queued предотвращает запуск; running cancel сначала `cancel_requested`, затем `cancelled` только после подтверждения owner. Late completion от старого owner отвергается.
- [ ] Хранить metadata в Redis, `self.tasks` оставить bounded cache, а не authority. Все переходы CAS по revision + token. Worker проверяет cancel между стадиями и перед side effect; действующий provider scope отменяется/ожидается тем же owner.
- [ ] Ввести terminal metadata retention 24 h и idempotency tombstone 24 h. У succeeded/cancelled/permanent failure/unknown payload удалять сразу. Только для безопасно повторяемого failed/expired оставить закрытый retry payload максимум 15 минут после terminal, ограничив его текущим privacy lease/account generation и общим byte budget; затем retry capability исчезает. Публичный snapshot содержит только очищенный summary. Не удалять running metadata по общему TTL; зависшее владение обрабатывает recovery.
- [ ] Recovery использует тот же attempt policy R3: исчерпанный бюджет → failed/dead letter metadata; неопределённая доставка → unknown, не автоматический repeat. Malformed payload сохранять изолированно и ограниченно с error counter без печати содержимого.
- [ ] Реализовать safe retry как **новый** job с `parent_task_id`, только из retry-eligible failed/expired с ещё доступным retry payload; исходный terminal неизменяем. Новый job получает новый срок и повторно проверяет текущие privacy права. Test: после expiry payload, account erasure или exhausted grant сервер отклоняет retry независимо от stale UI. `unknown_delivery` повторяется только как отдельно подтверждённая ручная отправка с новым содержимым/operation ID, вне кнопки retry failed.
- [ ] Проверить privacy/account deletion: очереди, idempotency mapping и summaries не воскрешают данные удалённого аккаунта; применять текущие privacy leases/generations перед чтением и delivery. Добавить сценарий удаления пока job pending, не создавать второй механизм consent.

```powershell
uv run --locked pytest tests/test_queue_job_lifecycle.py tests/test_queue_admission.py tests/test_queue_outcomes.py tests/test_redis_queue.py --override-ini="addopts=" --timeout=30 -q
```

### R4c. Ограничения объёма, delayed retry и возраст

**Finding:** Q04. **Зависимость:** R4b. **Файлы:** создать `app/queue_scheduling.py`, `tests/test_queue_scheduling.py`; изменить `queue_store.py`, `queue.py`, `deferred_response.py`, конфигурацию и env registry при добавлении env options.

**Контракт:** `QueuePolicy(max_jobs, max_payload_bytes, max_total_payload_bytes, max_age_seconds, max_attempts)`; `next_attempt_at(attempts_started, now, retry_after, random_value)`. Для первой версии deferred lane: 100 nonterminal jobs, 256 KiB/job, 8 MiB total payload, max age 900 s, max attempts 3. Эти defaults — консервативное предложение для замены безлимитного backlog, не доказанный оптимум.

- [ ] Тестировать exact limit и limit+1 для job/bytes, включая UTF-8 multi-byte текст; running/delayed занимают capacity. Reject до дорогой генерации возвращает reason, а не обещание отложенного ответа. Oversize history не резать молча до изменения смысла: либо существующая безопасная summary, либо explicit reject.
- [ ] Enqueue deferred с `not_before = now + 15 s`; удалить ожидания cooldown из handler. Scheduler атомарно переносит due IDs в ready, workers не спят на будущих jobs. На фазу cooldown не создавать отдельный многолетний sleeping task на job.
- [ ] Backoff после retryable: `min(60 s, 15 s * 2**(attempts_started-1))` + jitter 0–20%; `Retry-After` может увеличить задержку, но не deadline. При невозможности следующей попытки до deadline — expired. Clock/random инъецируются для тестов.
- [ ] Учитывать total serialized bytes атомарно с admission/terminal transitions и expiry retained retry payload из R4b; общий byte limit включает эти retained payload. Повторный ACK/cancel/recovery не уменьшает счётчик дважды. Сверять индекс и counters после restart/reconcile без вывода payload.
- [ ] Тест: 3 deferred jobs на cooldown не занимают 3 workers; готовая maintenance job может выполниться. Deadline/attempts ограничивают последовательный отказ и recovery.
- [ ] Выполнить focused scheduling/queue tests и Lua capacity contracts. Зафиксировать structured events rejected/expired/retry_scheduled и oldest age для O1/O3.

### R4d. Workload lanes и совместимость выпуска

**Findings:** Q04/Q06/D10. **Зависимость:** R4c. **Файлы:** `queue_contracts.py`, `queue_store.py`, `queue.py`, `tests/test_queue_scheduling.py`, `docs/runtime-controls.md`; создать `tests/test_queue_lane_isolation.py`.

**Контракт:** queue core поддерживает отдельные ready/delayed indexes по `lane` и явно назначенные worker pools. Начальные lanes: `deferred` с текущими 3 workers, `maintenance` с 1 worker, `daily` с 2 workers после D6. Конкурентность image work внутри daily ограничена одним выделенным permit. Policy/counters каждого lane независимы.

- [ ] Ввести lane registry с allowlist task kinds; неизвестные kind/lane отклоняются до записи. Не отдавать arbitrary Python callable/URL через admin API.
- [ ] Закрепить тест: заполненная/приостановленная daily lane не отнимает deferred workers; strict priority действует внутри lane; равный приоритет FIFO. Maintenance получает свой slot при непрерывном deferred потоке.
- [ ] Пауза admission и пауза claim — разные поля store с ревизией: первая запрещает новые enqueue, вторая оставляет running завершаться. `drain` = запрет новых enqueue + продолжение claim до пустой очереди. Не делать cancel-all неявным действием pause.
- [ ] Подготовить versioned payload и namespace compatibility тест: старый worker не читает новый формат. Обновить runbook drain/recovery/rollback, сохранить legacy recovery только как явно вызываемую операцию. Production cutover не запускать.
- [ ] Провести конечный synthetic workload: mixed lanes, 10 000 enqueue attempts, ограниченные jobs/bytes/tasks, backend outage и recovery. Проверять максимумы, FIFO и отсутствие leaked tasks; не предъявлять fake-provider throughput как скорость реальных моделей.
- [ ] Реализовать оставшиеся typed фасады для O3: `list_jobs(*, lane: str | None, state: JobState | None, cursor: str | None, limit: int = 50) -> JobPage`, где JobPage содержит items и next_cursor; `retry_job(task_id: str, *, expected_revision: int, actor: str, operation_id: str) -> QueueAdmission`; `set_lane_state(lane: str, *, expected_revision: int, admission_paused: bool, claim_paused: bool, actor: str, operation_id: str) -> LaneSnapshot`. LaneSnapshot содержит revision, оба paused поля и policy limits. Page size ≤100, payload не возвращается. Повтор operation_id идемпотентен; конфликт reuse с другим command body отклоняется.
- [ ] Пройти focused runtime suite и изолированные Redis contracts; закрепить signatures и значения enum в runbook, чтобы O3 не вводил второй набор состояний.

**Выход runtime-плана:** UI может читать правдивые snapshots и вызывать ограниченные CAS-команды. Это необходимое основание новых controls, а не разрешение немедленно включить массовые действия на production.
