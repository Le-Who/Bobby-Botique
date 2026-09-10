# Logging Overhaul Implementation Plan

> **For agentic workers:** использовать `executing-plans` для последовательной реализации с checkpoints. `subagent-driven-development` допустим только при отдельном разрешении пользователя на делегирование. Шаги отмечать checkbox. Этот документ сам по себе не разрешает внедрение, commit, push, deploy или работу с production данными.

**Goal:** сделать историю каждого запроса/фонового задания связной, структурированной, безопасно подробной и пригодной для расследования человеком и агентом.

**Architecture:** совместимый stdlib/structlog envelope → producer-side context/sanitization → bounded queue/writer → NDJSON stdout → контролируемое хранение и read-only incident export. Инструментирование выполняется на существующих границах ingress, provider attempts, response delivery, storage и jobs; бизнес-маршрутизация и владельцы ответов сохраняются.

**Tech Stack:** Python `>=3.14,<3.15`, `uv==0.12.6`, текущие stdlib logging/contextvars/queue/threading, structlog и pytest. Обязательный baseline не требует новой runtime-зависимости. Alloy/Loki/Grafana и OTel — отдельно включаемые последующие этапы.

**Spec:** [Аудит и целевая спецификация](../../logging-audit-2026-09-10.md), особенно разделы 6–11. Прочитать оба документа целиком перед реализацией.

**Implementation note (2026-09-10):** baseline implementation is present in the
same branch as this plan. Unchecked boxes remain a reusable acceptance checklist,
not an assertion that the corresponding code is absent. Optional external
collector/OTel work in Task 14 and live VPS retention/canary validation remain
separately authorized operational work.

| Tasks | Current branch evidence |
| --- | --- |
| 1–4 | Unified envelope, producer sanitization, shared context, bounded writer and fault-injection tests |
| 5–6 | Typed provider attempts, key identity, immutable delivery mapping and recovery/error ownership |
| 7–8 | Portable job execution context plus DB/cache/concurrency/state/context/memory lifecycle events |
| 9–10 | Critical direct-workload/Live/research coverage and bounded operational Prometheus metrics; low-risk unmigrated messages remain `legacy.log` |
| 11–12 | Release metadata, Docker rotation, bounded CI diagnostics, incident CLI, event catalog and runbook |
| 13 | Local gates and synthetic benchmarks complete; isolated integration services and live VPS canary not run |
| 14 | Deliberately deferred pending separate infrastructure authorization |

## Global Constraints

- Baseline checkout: `fb8f6166668a673500946a76b665820d27d9fab8`, 2026-09-10. Сверить символы и current checkout; номера строк в аудите исторические.
- Runtime: Python `>=3.14,<3.15`; pinned package manager: `uv==0.12.6`.
- Перед изменениями `git status --short`; сохранять пользовательские правки. Не commit/push/deploy без отдельного запроса.
- Применить AGENTS.md и актуальные nested agreements; не читать `.env`/production logs ради тестов.
- Сохранить response_delivery ownership, immutable outcomes/receipts, typed provider events, state/consent/lease/transaction invariants.
- Не менять retry count, timeout, key rotation, model precedence, deferred ack/retry, Telegram UI/публикацию в рамках logging refactor.
- `TELEGRAPH_PUBLICATION_ENABLED=false` и `private_content` gates не ослаблять.
- Все durations monotonic; timestamp UTC RFC3339; JSON UTF-8, одна строка на event.
- Нет полных credentials, stack locals, full messages/headers/body или base64. Для каждого фактически выбранного provider key обязательны `key_suffix` (минимум последние 4 символа) и fingerprint как в спецификации; raw user/chat IDs допустимы в закрытом operational sink; контент только по утверждённой policy.
- Не добавлять сеть/DB в log handler; не превращать ошибку logging в сбой бизнес-операции.
- Новые integrations выключены по умолчанию до отдельной эксплуатационной проверки.
- Документы UTF-8; до/после правок `python -X utf8 scripts/check_encoding.py`, после — `git diff --check`.

## Как пользоваться планом

Это один logging program с независимо проверяемыми этапами, а не разрешение
одновременно переписать 1500 вызовов. Порядок: **1–10 → 12 → 11 → 13**;
incident CLI из задачи 12 нужен для безопасного CI excerpt в задаче 11.
После каждого checkpoint фиксировать проверенные сценарии и diff. Задача 14 опциональна.
Не оставлять baseline полуработающим: API facade и stdlib callers должны продолжать
писать usable logs, пока соответствующий модуль ещё не мигрирован.

Общий цикл каждой задачи:

1. Написать указанный regression test на artificial inputs.
2. Запустить focused tests и увидеть ожидаемый FAIL новой проверки.
3. Реализовать только обозначенные изменения и сохранить поведение приложения.
4. Повторить focused tests; проверить реальный serialized output, не только mock calls.
5. `git diff --check`; записать evidence. Commit — только если отдельно разрешён.

Команда focused test: `uv run --locked pytest <existing-or-created-test-paths> --override-ini="addopts=" --timeout=30`.
Все новые paths ниже являются планируемыми, не заявлением об их существовании.

## Карта файлов и границы ответственности

| Файл | Ответственность |
| --- | --- |
| app/observability/__init__.py | Узкий публичный API без startup/config side effects |
| app/observability/schema.py | JSON types, envelope, bounds, event validation |
| app/observability/events.py | emit, exception evidence/error IDs, operation scopes |
| app/observability/context.py | Единый context и token-reset scopes, portable job context |
| app/observability/redaction.py | Allowlist/pattern/exact-secret scrubbing, URL handling, safe bounds |
| app/observability/config.py | Явный parse logging env без импорта app.config |
| app/observability/pipeline.py | Stdlib/structlog normalization, source/extra/exception snapshot |
| app/observability/writer.py | Byte/count bounded queue, thread writer, loss/drain/failure state |
| app/observability/provider_events.py | Mapping typed events/attempt observations без SDK calls |
| app/observability/metrics.py | Bounded operational counters/histograms/snapshot без основной DB |
| app/observability/incident.py | Read-only select/link/redact/render incident evidence |
| app/utils/logging_config.py | Compatibility facade для setup/get_logger; не второй pipeline |
| app/request_context.py, app/tracing.py | Compatibility adapters над одним context store |
| app/utils/api_logger.py | Compatibility API без JSON-in-message; миграция duration semantics |
| scripts/log_incident.py | CLI для incident export; без импортов app.config/бота |
| docs/logging.md, docs/log-events.md | Operator/agent runbook и машинно-стабильный event catalog |
| tests/observability/ | Contract/fault injection/behavior-neutrality tests |

Не создавать общую абстракцию «всё вокруг async оборачиваем декоратором»:
boundaries должны знать typed business outcome. Декоратор без исключения не
отличает FailedDelivery от успешного ответа и returned job от выполненной задачи.

## Предлагаемые интерфейсы для задач

Следующие signatures — контракт проектирования. Реализовать тела в указанных
файлах; не копировать Protocol как бессмысленную отдельную runtime абстракцию.
Они определяют названия и типы, используемые в snippets ниже.

```python
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type Fields = Mapping[str, JsonValue]
type Level = Literal["debug", "info", "warning", "error", "critical"]

@dataclass(frozen=True, slots=True)
class LogContext:
    request_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    user_id: int | None = None
    chat_id: int | None = None
    task_id: str | None = None
    execution_id: str | None = None

@dataclass(frozen=True, slots=True)
class OperationResult:
    outcome: str
    reason_code: str | None = None
    error_id: str | None = None

class ObservabilityAPI(Protocol):
    def current_context(self) -> LogContext: ...
    def request_scope(self, *, request_id: str | None = None,
                      user_id: int | None = None,
                      chat_id: int | None = None) -> AbstractContextManager[LogContext]: ...
    def span_scope(self, name: str, **fields: JsonValue) -> AbstractContextManager[LogContext]: ...
    def emit(self, event: str, *, level: Level = "info",
             message: str = "", **fields: JsonValue) -> None: ...
    def record_exception(self, event: str, error: BaseException, *,
                         operation: str, level: Level = "error",
                         fields: Fields | None = None) -> str: ...
    def export_job_context(self) -> dict[str, JsonValue]: ...
    def restore_job_context(self, data: Fields, *, task_id: str,
                            execution_id: str) -> AbstractContextManager[LogContext]: ...
```

`request_scope` создаёт request_id/trace_id=32 hex при отсутствии ID и новый span=16 hex;
нужна test-only возможность явного ID, legacy arbitrary ID сохраняется как
`legacy_request_id`, не как W3C trace ID. `span_scope` наследует trace/request/actor,
создаёт дочерний span; name попадает в operation. Смена actor только на входной
доверенной границе. `emit` не принимает произвольные Python objects: legacy normalization
заменяет неизвестные объекты именем типа, а не `repr` с потенциальными секретами.
`record_exception` всегда возвращает error_id даже при недоступном sink.

---

## Задача 1. Regression harness и восстановление единого output

**Files:** создать `tests/observability/__init__.py`, `test_pipeline.py`,
`app/observability/__init__.py`, `schema.py`, `pipeline.py`;
изменить `app/utils/logging_config.py`.

**Consumes:** existing setup/get_logger/stdlib call sites.
**Produces:** одинаковый envelope для stdlib, structlog, third-party logs;
`legacy.log` для немигрировавших строк. Пока synchronous writer допустим только
как промежуточный checkpoint, до rollout обязательна задача 4.

- [ ] В subprocess test выполнить текущий setup, stdlib info(extra), structlog info,
  активный raise/except/logger.exception и исключение с cause. Проверять строки stdout.
- [ ] Добавить следующий смысловой assertion для stdlib JSON; на baseline он падает:

```python
event = parsed_stdout[-1]
assert event["request_id"] == "a" * 32
assert event["level"] == "info"
assert event["logger"] == "audit.stdlib"
assert event["operation"] == "probe"
assert event["timestamp"].endswith("Z")
assert event["schema_version"] == 1
```

- [ ] В configured ProcessorFormatter нормализовать foreign records через общий
  pre-chain, ExtraAdder и source fields. Structlog path применять тот же envelope
  ровно один раз; renderer только сериализует. Удалять `_record/_from_structlog`
  перед JSON. Вспомогательные context fields захватывать на producer.
- [ ] Для stdlib сохранять `record.created/name/levelname/pathname/funcName/lineno`;
  не вычислять source как pipeline.py. Для event wrapper применять корректный stacklevel.
- [ ] Exception преобразовывать через переданный error/record.exc_info, не читать
  только текущее sys.exc_info. Сериализовать frames/causes/ExceptionGroup, без locals.
- [ ] Убедиться, что JSON не содержит `<traceback object`, null time/level либо
  `"operation"` внутри текстового JSON. Unicode — реальные символы, newline escaped.
- [ ] Проверить idempotent setup в отдельном subprocess и отсутствие дублирования
  root/named handlers. Устаревшие setup_* stubs либо явно deprecated, либо делегируют.

**Tests:** новый `test_pipeline.py` + существующие `test_request_context.py`,
`test_api_logger_request_id.py`, logging case в `test_audit_fixes.py`.
Не менять тесты так, чтобы они просто приняли текущую потерю полей.

## Задача 2. Общая очистка, bounded content и согласованная политика

**Files:** создать `redaction.py`, `config.py`, `events.py`,
`tests/observability/test_redaction.py`, `test_config.py`, `test_schema.py`;
изменить `pipeline.py`, `app/config.py:update_setting`,
`app/admin_alerts.py`, risky call sites из L09.

**Consumes:** schema/pipeline задачи 1.
**Produces:** `emit`, `record_exception`, безопасный snapshot; конфигурация ниже.

| Setting | Default / поведение |
| --- | --- |
| LOG_FORMAT | json по умолчанию; text явно для local; один resolver без эвристики DATABASE_URL |
| LOG_LEVEL | INFO; invalid value = configuration warning + INFO, без печати env |
| STRUCTURED_LOGGING / LOG_PRETTY | Compatibility aliases; LOG_FORMAT имеет приоритет, конфликт логируется |
| SERVICE_NAME / APP_ENV / APP_RELEASE | gemaibotv2 / unknown / unknown; deployed release отдельно передаёт workflow |
| LOG_CONTENT_MODE | metadata; preview явно включает §7 spec, не меняется через DEBUG |
| LOG_EVENT_MAX_BYTES | 32768, hard ceiling; не допускает безлимит |
| LOG_QUEUE_MAX_EVENTS / LOG_QUEUE_MAX_BYTES | 4096 / 8388608, валидировать положительные пределы |
| LOG_DIAGNOSTIC_REQUEST_ID / LOG_DIAGNOSTIC_USER_ID | Нет scope по умолчанию; нужен хотя бы один selector |
| LOG_DIAGNOSTIC_SUBSYSTEM / LOG_DIAGNOSTIC_UNTIL / LOG_DIAGNOSTIC_INCIDENT_ID | Для активации все три обязательны; until RFC3339 ≤ now+15min |
| LOG_DIAGNOSTIC_KEY_SUFFIX | false; только long provider key, не generic secret |

Не создавать admin UI для diagnostics в baseline: scope задаёт оператор startup
конфигурацией; срок проверяется на каждом событии, expiry summary — один раз.

- [ ] Parametrized тесты с **искусственными** token/DSN/password в msg, % args,
  nested extra, exception causes/groups, headers, escaped JSON, URL query/userinfo,
  Telegram `/botTOKEN/`, bytes, object с опасным __repr__, cyclic container.
- [ ] Пример обязательного теста одного sink и отсутствия утечки:

```python
def test_secret_in_exception_is_not_serialized(captured_events):
    from app.observability.events import record_exception
    err = ValueError("Authorization: Bearer fake-secret-value-for-tests")
    error_id = record_exception("provider.attempt_failed", err, operation="synthetic")
    wire = captured_events.ndjson()
    assert "fake-secret-value-for-tests" not in wire
    assert captured_events.rows[-1]["error_id"] == error_id
    assert captured_events.rows[-1]["exception"]["type"] == "ValueError"
```

`captured_events` — новый fixture с реальным pipeline, in-memory writer и
rows/ndjson(); сбрасывает setup/context после теста. Не заменять formatter MagicMock.

- [ ] Реализовать recursive safe conversion: reject/replace unknown objects,
  cycle/depth/item bounds; секретные поля заменять целиком; truncate после scrubbing.
  Строковый fallback не должен включать аргументы ошибки сериализации.
- [ ] Protect reserved fields: fake `extra.request_id/level/event_id` не перекрывает
  доверенный envelope; записывать безопасный collision indicator.
- [ ] Перед логированием классифицировать content по handler/path, не по «есть ли
  слово password». Содержимое `/addkey`, auth, natal, LTM, documents, group data
  запрещать целиком; preview только approved normal private text после auth.
- [ ] Удалить raw logs `msg_roles.py`, `cb_roles.py`, cache token/URL, provider body,
  generic config value; заменить counts/type/reason и policy-controlled preview.
  Не хранить копию raw payload ради deferred rendering.
- [ ] Alerts/error persistence/export использовать тот же sanitizer; новый secret
  key хранить только там, где он уже нужен приложению, а не копировать всю env в logger.
  Если нужен exact-match registry, обновлять при key add/rotation/delete, ограничить
  размер/срок, закрыть repr; не полагаться только на registry для unknown secrets.
- [ ] Проверить TTL, all-selectors-AND, запрет global debug exposure, min key length,
  UTF-8 byte caps, cancellation, конфликт env aliases, отсутствие чтения app.config.

**Checkpoint:** L01/L02/L09 закрыты contract tests; full sensitive contents не появились
в новом output. Preview пока metadata-only до утверждения deployment policy.

## Задача 3. Единый context и propagation на входах

**Files:** создать `context.py`, `tests/observability/test_context_lifecycle.py`,
`test_ingress.py`; изменить `app/request_context.py`, `app/tracing.py`,
`app/update_processor.py`, `bot.py`, `app/utils/decorators.py`,
`app/handlers/messages.py`, `cb_ai_actions.py`, `cb_voice.py`,
`app/web.py`, `app/web_miniapp.py`.

**Consumes:** request_scope/span_scope contract; emit.
**Produces:** контекст установлен до любых state/auth/lock/provider действий;
finish event на все controlled exits.

- [ ] Проверить nested restore и concurrent users:

```python
with request_scope(request_id="1" * 32, user_id=42) as outer:
    with request_scope(request_id="2" * 32, user_id=84):
        assert current_context().user_id == 84
    assert current_context() == outer
assert current_context().request_id is None
```

- [ ] Оборачивать `UserScopedUpdateProcessor.process_update` до user lock;
  измерить lock wait без изменения порядка admission и coroutine ownership.
  HTTP acceptance scope создавать в Quart before_request/teardown_request;
  WS scope — на handshake/session и child spans на turn, без покадровых logs.
- [ ] Webhook transport → PTB queue: явно передать связь HTTP request/update scope.
  Предпочтительно small bounded map update_id→origin context с TTL=5min, max=4096,
  удалением при consume/reject; capacity/expiry видны. Не модифицировать SDK Update
  скрытыми attributes. При отсутствии записи создать новый trace и `origin_missing`.
  Polling не зависит от этой map.
- [ ] Inbound X-Request-ID не считать доверенным: server ID новый; client ID при
  необходимости отдельное поле ≤64 printable ASCII без control chars.
- [ ] Внутренние decorators/handlers перестают перезаписывать существующий request ID.
  Прямые вызовы тестов/helpers без ingress получают совместимый fallback scope.
  `clear_*` adapters не сбрасывают родительский scope; тестировать token restore.
- [ ] Safe_handler/global_error_handler должны сообщать результат owner scope;
  PTB может обработать exception и нормально вернуть coroutine. Поэтому
  `await coroutine` без raise не считать автоматически успешным бизнес-запросом.
  Аналогично rejected/duplicate/absorbed/partial/deferred результаты помечать явно.
- [ ] Проверить message, edit, inline/chosen inline, callback, unauthorized,
  webhook invalid JSON/secret/duplicate/full queue, HTTP 404/500, WS timeout/disconnect,
  scheduled operation без пользователя; user claims становятся trusted только после auth.
- [ ] `X-Request-ID` у исходящих adapters теперь opaque ID; существующие тесты
  headers сохранить, legacy arbitrary IDs поддерживать только во внутреннем compat.

**Tests:** новые context/ingress; `test_update_processor.py`, `test_tracing.py`,
`test_decorators.py`, `test_request_id_headers.py`, `test_webhook_dedupe.py`,
`tests/e2e/test_webhook_lifecycle.py`. Не запускать live webhook.

## Задача 4. Nonblocking bounded writer и startup/shutdown

**Files:** создать `writer.py`, `tests/observability/test_writer.py`,
`test_bootstrap.py`; изменить `pipeline.py`, `logging_config.py`, `bot.py`.

**Consumes:** очищенный producer snapshot с source/context/exception data.
**Produces:** один writer owner, enqueue без blocking I/O, явные потери и drain.

- [ ] Искусственно задержать sink; подтвердить, что unrelated asyncio heartbeat
  продолжает выполняться, queue bytes/events не растут сверх cap.
- [ ] Queue хранит snapshot, не LogRecord с живым traceback/locals/context objects.
  В in-process queue можно передавать immutable serialized bytes после safe snapshot;
  измерить producer CPU. Не применять stock prepare до сохранения exception fields.
- [ ] Один worker thread сериализует/пишет stdout; все application/named loggers
  идут через него. Особые уровни third-party сохраняются, не separate hidden sinks.
  Default stdlib warning/error всегда получает общую очистку.
- [ ] Admission policy: reserve 25% count/bytes для WARNING+; DEBUG/INFO не занимают
  reserve, WARNING+ могут вытеснять низкий уровень. Если важных слишком много,
  drop с counters by level/reason; counters не логируют сами себя рекурсивно.
- [ ] Каждые ≤30s при потерях выдавать loss_summary, а не строку на каждый drop.
  После восстановления sink — counts/window/first_failure, не raw previous event.
  Emergency stderr ограничить по частоте и размеру; broken pipe не вызывает retry loop.
- [ ] Startup bootstrap настроить **до** app.config/прочих модулей, которые могут
  логировать при импорте. Не импортировать Settings ради logging; сообщения fatal
  validation должны содержать имя невалидного параметра, не secret value.
- [ ] sys.excepthook/threading.excepthook/asyncio exception handler и warnings
  привести к очищенному envelope, сохраняя existing lifecycle exception propagation.
  SystemExit/KeyboardInterrupt/cancellation не выдавать за неожиданный ERROR.
- [ ] Idempotent reconfigure закрывает только owned handlers/listener; не удаляет
  чужие тестовые/embedding handlers молча. File sink либо deprecated, либо rotating
  и получает те же events; failed file open виден через fallback event.
- [ ] Drain timeout ≤3s в штатном shutdown; тестировать full queue при stop, repeated
  stop и sink failure. При kill не обещать flush. Отдельно проверить daemon/non-daemon
  thread lifetime, чтобы зависший writer не мешал завершению процесса.

**Tests:** byte caps, 4096 count cap, overflow priority, context snapshot до смены
scope, GC traceback release, broken pipe, write exception, interrupted shutdown,
repeated setup, JSON line integrity при нескольких producer threads.

## Задача 5. API facade, provider attempts, retries и races

**Files:** создать `provider_events.py`, `tests/observability/test_provider_events.py`;
изменить `app/utils/api_logger.py`, `app/providers/router.py`, `base.py`,
`gemini.py`, `openrouter.py`, `opencode.py`, `freetheai.py`, `stream_types.py`,
`app/resilience_policy.py`, `app/utils/network.py`, `app/search_services.py`.

**Consumes:** emit/record_exception/context scopes, typed terminal events.
**Produces:** stable provider.attempt_* events, error_id для transformed exceptions.

- [ ] Покрыть synthetic 429→success, 503→model fallback, missing/decrypt-failed key,
  malformed stream, timeout before/after text, race winner/loser cancellation.
- [ ] Provider boundary создаёт новый attempt_id на **каждый** реальный HTTP/SDK call;
  router создаёт race_id и round fields. Retry wrapper пишет отдельный decision,
  но не дублирует attempt_started уже инструментированного adapter.
- [ ] Реализовать mapping terminal → fields без повторного парсинга diagnostic:

```python
fields = {
    "reason_code": terminal.code.value,
    "failure_phase": terminal.phase.value,
    "retry_disposition": terminal.retry.value,
    "key_disposition": terminal.key.value,
}
emit("provider.attempt_finished", level="warning", outcome="failed", **fields)
```

- [ ] Для caught exception `record_exception` до conversion; добавить optional
  `error_id: str | None = None` к StreamFailed и передавать ссылку, не Python exception.
  Не логировать traceback повторно при relay того же error_id. Diagnostic очищать
  до truncation, включая HTTP-specific branches.
- [ ] Usage/actual route/finish reason читать из terminal; unknown usage = null;
  стоимость только при наличии источника тарифа, в baseline не вычислять.
  TTFT стартует до provider attempt; отдельно общий request queue wait.
  `provider_request_id` — allowlisted response header/SDK metadata, не локальный ID.
- [ ] Key fingerprint согласовать с repos/keys; legacy raw-prefix KEY_EVENT заменить.
  Для каждого request/attempt/error с фактически выбранным ключом обязательно писать
  `key_suffix`: последние 4 Unicode code points (весь ключ, если он короче четырёх),
  отдельно от fingerprint; сохранить key source=user/admin/env где этот факт доступен.
  Для отсутствующего ключа писать `key_present=false`, `key_suffix=null`; не выдавать
  конец hash за suffix сырого ключа. Общий scrubber разрешает это только в поле
  `key_suffix`, но удаляет тот же фрагмент из message/exception/body/URL.
- [ ] APILogger больше не json.dumps поля в message. Добавить монотонный timing
  handle (dataclass с started_ns и attempt_id) и мигрировать все callers вместе;
  compatibility log_response для старого float временно пометить legacy timing,
  не вычитать epoch float из perf_counter. Удалить compatibility после отсутствия callers.
- [ ] Отделить `telegram.update` timing от Telegram RPC timing. Pair validation для
  всех early returns; API facade user fields не могут перекрывать context envelope.
- [ ] Сверить exact calls/counts/arguments и terminal event sequence с baseline mocks:
  instrumentation не добавила network calls, retries, sleeps либо context mutation.

**Tests:** `test_provider_events.py`, `test_provider_router.py`, `test_gemini_provider.py`,
`test_openrouter_provider.py`, `test_opencode_routing.py`, `test_provider_stream_types.py`,
`test_resilience.py`, `test_api_logger_request_id.py`.

## Задача 6. Generation/delivery terminal summary и error ownership

**Files:** изменить `app/response_delivery/delivery.py`, `coordinator.py`,
`renderer.py`, `app/utils/messaging.py`, `app/errors.py`, `bot.py:global_error_handler`;
создать `tests/observability/test_delivery_events.py`.

**Consumes:** typed outcome/receipt, generation/attempt error IDs.
**Produces:** единственный delivery.finished per invocation и request-level outcome.

- [ ] Тесты всех outcome types плюс transport exception; один terminal summary,
  включая normal return с FailedDelivery. Cancellation имеет отдельный reason.
- [ ] Mapping из существующего outcome, не из success-флага декоратора:

```python
emit(
    "delivery.finished",
    generation_status="failed",
    delivery_status="sent",
    delivery_kind=outcome.receipt.kind.value,
    reason_code=outcome.error_code.value,
    message_ids=list(outcome.receipt.message_ids),
)
```

Этот snippet относится именно к FailedDelivery; Complete/Partial/Deferred имеют
разные mappings. Не обращаться к несуществующему error_code у CompleteDelivery.

- [ ] Facade владеет terminal logging для stream **и** deliver; renderer пишет
  recovery decisions (edit→send, Reader→split), coordinator — first visible/typed
  phase. Не дублировать terminal на каждом из трёх уровней.
- [ ] Сохранять ошибки transport с отдельным error_id и upstream generation error
  ссылкой: «AI failed и notice send failed» — две разные причины/последствия.
- [ ] Publication URL/reader UID не печатать как секретную ссылку: kind/storage ID
  в безопасном формате, host/publication_enabled/private_content flags достаточно.
- [ ] Проверить private-content gates, fallback sequence, final keyboard ownership,
  final_message/message_ids в receipt; не менять ни текст ни число transport RPC.
- [ ] Failed error notification в safe_handler/global handler писать bounded
  secondary event с original error_id, не молча `pass` и не recursive user notify.

**Tests:** новый delivery events + `test_response_coordinator.py`, `test_telegram_renderer.py`.

## Задача 7. Durable jobs, background tasks, scheduler и TTS

**Files:** изменить `app/queue.py`, `deferred_response.py`,
`utils/background_tasks.py`, `voice_engine.py`, `bot.py` job wrappers;
создать `tests/observability/test_job_context.py`, `test_job_outcomes.py`.

**Consumes:** export_job_context/restore_job_context и pipeline.
**Produces:** связные jobs после restart, различимые execution/business outcomes.

- [ ] Round-trip test portable context через queue JSON и новый worker:

```python
with request_scope(request_id="a" * 32, user_id=42):
    portable = export_job_context()
with restore_job_context(portable, task_id="synthetic-job", execution_id="b" * 32):
    assert current_context().request_id == "a" * 32
    assert current_context().task_id == "synthetic-job"
assert current_context().task_id is None
```

- [ ] Task добавить optional `observability_context` и schema version; serializer
  сохраняет только safe ID context, не весь ContextVar snapshot и не history.
  Старые payload без поля продолжают читаться, генерируют новый trace + origin_missing.
  Bounded validation и unknown-version fallback без поломки ack original bytes.
- [ ] `_worker` создаёт execution_id/span перед обработкой, reset в finally,
  связывает recover/retry/terminal events; тест двух users последовательно одним worker.
- [ ] Deferred failure dict логировать как `execution_outcome=returned`,
  `business_outcome=failed`; business outcome extractor только для известных task_type.
  Для неизвестного returned dict — business_outcome=unknown, не guessed success.
  Существующие ack/retry/status transitions не менять; отдельно записать технический долг.
- [ ] TaskManager получает optional operation/task name и metadata без breaking
  существующих submit callers; background child span наследует request. Singleton
  и independently constructed managers одинаково инструментированы.
- [ ] Cancelled task, capacity rejection, retry delay, exhausted, drain timeout,
  failed alert callback: explicit reason events без повторного traceback.
- [ ] Scheduled runs новый trace/run_id, due time/lag, aggregate selected/sent/failed/
  skipped; per-item failures с linked user/task/span. Periodic jobs не наследуют
  startup actor; TTS переносит origin через свою queue/job object.

**Tests:** новые tests + `test_task_queue.py`, `test_redis_queue.py`,
`test_background_tasks.py`, `test_voice_engine.py`, `test_scheduled_briefs.py`,
`test_horoscope_scheduled.py`. Redis mock round-trip недостаточен для live recovery;
реальный Redis тест только на отдельном disposable instance при отдельном разрешении.

## Задача 8. Storage, concurrency, context и memory evidence

**Files:** изменить `app/database.py`, `app/adapters/concurrency.py`, `app/state.py`,
`app/cache.py`, `app/context/assembler.py`, `compression.py`, `summarizer.py`,
`app/repos/memory_extraction.py`, `memory_consolidation.py`, `memory_graph_writer.py`,
`memory_consent.py`, `app/document_processor.py`, `app/documents/repository.py`;
создать `tests/observability/test_storage_events.py`.

**Consumes:** spans/emit; exact current transaction/lease contracts.
**Produces:** latency phase breakdown и состояние деградации без content/SQL params.

- [ ] Fake pool acquisition delay и separate query delay должны давать разные поля;
  SQLSTATE/failure stage фиксируется до user-friendly conversion.
- [ ] В db_query wrapper добавить optional stable operation name; при отсутствии
  имя `db.query` + SQL operation kind без SQL text/parameters. Не monkeypatch asyncpg
  глобально и не логировать каждую быструю query на INFO.
- [ ] Для caller-owned connection/graph writer инструментировать саму transaction
  boundary и write phase, без новых acquire/commit/provider calls.
- [ ] Контракт события успешной атомарной записи:

```python
emit("memory.graph_write_finished", outcome="committed", source_count=3,
     node_count=2, edge_count=1, consent_epoch=7, duration_ms=12.5)
```

Числа в примере искусственные; в runtime брать фактический plan/result, emit после
подтверждённого commit owner. Внутри transaction писать `prepared/applied`, не `committed`.

- [ ] Lease lost/consent revoked/stale epoch дают skipped/cancelled reason, не generic
  model failure. Source IDs — только нужные для связи, массивы ограничены.
- [ ] Redis/local fallback, cache namespace hit/miss, state hydration/persist/coalesced
  write/dirty marker, lock_wait/global_wait: без полных ключей cache с content/token.
- [ ] Context assembly: budgets/turn count/summary strategy/recalled count и failure
  stage; LTM facts/embedding vectors/doc bodies/filenames с личными данными не выводить.
- [ ] Ошибки extraction/rendering имеют parser/stage/type, размеры и source ID;
  metrics сохранения и business persistence success не путать с фактом provider response.

**Tests:** fake storage events + `test_state_lifecycle.py`, `test_concurrency.py`,
`test_context_assembler.py`, `test_memory_graph_writer.py`,
`test_memory_extraction_provenance.py`, `test_memory_consolidation_safety.py`.
Не запускать DB migration/status/check как диагностику logging.

## Задача 9. Полное покрытие specialized workloads и remaining callers

**Files:** `app/providers/imagen_provider.py`, `pollinations.py`, `freetheai_image.py`,
`freetheai_audio.py`, `tts.py`, `elevenlabs_tts.py`; `app/core/agentic.py`,
`app/search_jina.py`, `app/intent_router.py`; `app/games/`, `app/natal/`,
`app/handlers/`, `app/web_miniapp.py`; новый `tests/observability/test_workload_events.py`.

**Consumes:** shared attempt/delivery/job/context contracts.
**Produces:** coverage matrix §8 spec закрыта, оставшиеся legacy.log осознанно учтены.

- [ ] Inventory каждого call site: migrated event / legacy diagnostic / removed
  duplicate / expected silence. Не массовая замена `.info` без семантики.
- [ ] Parameterize minimum contract для image/STT/TTS/embeddings/Live/daily game
  direct SDK paths: start, outcome, provider/model, duration, origin и failure evidence.
- [ ] Research tool events на каждую логическую tool execution, не на токен;
  tool_call_id/iteration/budget/reason/result count; safe host вместо полного URL.
- [ ] Live WebSocket: session opened/turn finished/disconnected, transport mode,
  idle timeout/close code/reconnect decision, byte counters; audio frames/content
  не логировать. DoS disconnects aggregate, не один ERROR на каждый frame.
- [ ] Daily/broadcast/natal: run/recipient/report IDs, generation/validation/send
  отдельно; обязательные safety denials content policy должны быть проверены.
- [ ] Silent except inventory классифицировать: expected control flow — молчание или
  DEBUG metric; real degraded fallback — WARNING+reason; unexpected terminal — ERROR
  и stack. Не превращать 105 except-pass механически в 105 alerts.
- [ ] Root `logging.*` в затронутых модулях заменить именованным logger или emit
  с корректным source; низкорисковые неинструментированные строки допускаются
  как legacy.log с envelope, но не в critical lifecycle.

**Acceptance matrix (parameterized integration-style tests):**

```python
required = {"event", "timestamp", "request_id", "level", "operation"}
for row in recorded_workload_events:
    assert required <= row.keys()
assert any(row["event"].endswith("finished") for row in recorded_workload_events)
assert not any("base64" in row for row in recorded_workload_events)
```

Дополнительно semantic assertions по каждому workload из spec, а не только presence
keys: лимит/отмена/неверный ответ и фактическое число SDK calls.

## Задача 10. Метрики, health и alerts, согласованные с событиями

**Files:** создать `app/observability/metrics.py`, `tests/observability/test_operational_metrics.py`;
изменить `app/prometheus.py`, `app/metrics.py`, `app/admin_alerts.py`,
`app/web.py:api_errors/prometheus_metrics`, `bot.py:monitoring_task`,
`app/circuit_breaker.py`, `app/degradation.py`.

**Consumes:** owner-emitted events, не парсинг stdout/message.
**Produces:** process-local bounded snapshot и честные counters/health.

- [ ] Regression: искусственно записать один provider attempt и failure;
  exporter обязан показать nonzero соответствующих counters и monotonic uptime.
  Никаких getattr на отсутствующие `_api_calls/_errors/_start_time`.
- [ ] Отдельный OperationalMetrics без DB: counters по provider/validated model/
  outcome, request/job/delivery outcome, logging drops/failures; bounded histograms
  request latency, TTFT, queue wait, pool wait. Настоящие provider-model pairs
  получать при регистрации attempt, не cross-product marginal counters.
- [ ] Label values ограничить каталогом/enum и `other`; request/user/error IDs
  никогда не labels. Text exporter корректно escapes backslash/quote/newline.
- [ ] Existing MetricsCollector остаётся продуктовой статистикой; runtime counters
  не инициализировать historical DB totals без отдельной согласованной semantics.
  Ввести queue bound/overflow counts и не recursive logging в processor failure.
- [ ] ERROR logging не равно error counter всех запросов: считать именно semantic
  terminal request/job events. Один retry failure не три failed requests.
- [ ] Dashboard recent errors получает bounded sanitized incident summaries с
  error_id/request_id/release; старые persisted rows поддерживаются как legacy.
  Не требовать SQL migration для baseline и не писать все operational logs в DB.
- [ ] Alert payload: fingerprint, first/last seen, affected count, error_id/request_id,
  stage/release и безопасная summary. Без raw traceback/content в Telegram.
  Rate-limit per fingerprint + глобальный cap; подавление тоже считает количество.
- [ ] Отдельный independent external uptime alert — задача инфраструктуры: если
  Telegram недоступен или process dead, сам бот не гарантирует отправку alert.
- [ ] Health heartbeat описывает реальные dependency states, не unconditional
  All systems operational. INFO на переход/recovery, periodic unchanged — DEBUG.

```python
assert metrics.request_failed_total == 0  # recovered 429 не failed request
assert metrics.provider_failed_total == 1
assert metrics.provider_succeeded_total == 1
assert metrics.delivery_sent_total == 1
```

Эти свойства — ожидаемые summary в fixture; operational snapshot предоставить
через явный typed API, не через private attributes другого collector.

**Tests:** новые metrics tests, `test_metrics_snapshot.py`, `test_metrics_middleware.py`,
`test_admin_alerts.py`; failure/restore exporter tests без live DB.

## Задача 11. Хранение, release metadata, deploy logging и migration logs

**Files:** `.github/workflows/deploy.yml`, `Dockerfile`, `docker-compose.yml`,
`scripts/migrate.py`, `scripts/release_cloud_bot_api.py`, новый
`tests/observability/test_deployment_contract.py`, `docs/logging.md`.

**Consumes:** bounded NDJSON baseline, incident CLI из задачи 12 и approved retention policy.
**Produces:** explicit per-container logging limits, known release, safe CI excerpts.

- [ ] Перед редактированием workflow проверить актуальные инструкции GitHub Actions
  skill/documentation; это не отдельное разрешение запускать workflow/deploy.
- [ ] Передать `APP_RELEASE` равным разворачиваемому IMAGE_TAG/SHA в bot и migrate;
  не читать .git в runtime и не отправлять весь env в startup event.
- [ ] Container flags для минимальной конфигурации:

```bash
--log-driver local --log-opt max-size=20m --log-opt max-file=5
```

Применить осознанно к tg-bot/tg-api/cleanup/migrate (ephemeral migrate evidence
вывести в безопасный CI summary перед удалением). Compose заменить LOG_JSON
на канонический LOG_FORMAT. Не менять daemon-wide settings без отдельного согласия.

- [ ] Удалить raw `docker logs --tail ...` из failure-reporting веток CI; отдавать
  allowlisted summaries через safe incident CLI. Логи tg-api чужого формата
  передавать как unknown/opaque: только scrubbed bounded text либо metadata,
  не полагаться на app scrubber для чужого процесса.
- [ ] Если sanitizer недоступен на failed image: CI печатает container state/
  exit code/restart count/image SHA, но не raw tail. Полный анализ — оператором
  в защищённом хранилище. Sanitizer failure не отменяет исходный deploy failure.
- [ ] Scripts используют bootstrap pipeline без импорта app runtime, migration
  version/duration/outcome; ни DSN, ни SQL params. Проверять их logs искусственными
  exceptions/subprocess mocks, не выполнять миграции ради теста.
- [ ] Описать retention/spool/backup/ACL и доступ CI viewers. Локальная rotation
  не гарантирует долговременный incident archive после docker rm; до claims об
  архиве проверить сохранение collector/store через redeploy.
- [ ] Operator checklist только read-only, без полномасштабного `docker inspect`
  (он печатает env); точечные State/LogConfig/Config.Image fields разрешены после
  предоставления эксплуатационного scope. Disk limits измерить отдельно.

**Gate:** офлайн container smoke с fake config, synthetic exceptions/redaction;
live canary/deployment выполняется лишь по отдельному запросу пользователя.

## Задача 12. Incident CLI, event catalog и инструкция для агента

**Files:** создать `app/observability/incident.py`, `scripts/log_incident.py`,
`tests/observability/test_incident_export.py`, `docs/log-events.md`, `docs/logging.md`;
обновить `README.md`, `docs/README.md`, `docs/ARCHITECTURE.md`.

**Consumes:** NDJSON v1, optional legacy plain lines, source/release/error/job links.
**Produces:** `events.ndjson`, `incident.md`, `manifest.json`, deterministic offline export.

- [ ] CLI flags: `--input` (path или `-` stdin), `--request-id`, `--error-id`,
  `--user-id`, `--since`, `--until`, `--output`, `--include-identifiers`,
  `--include-content`, `--ci-summary`. User search обязательно ограничен временем.
  По request/error тоже применять declared input time bounds, не читать бесконечный stream.
- [ ] Streaming parser c line/event/total-byte limits, корректный Unicode, malformed
  lines с source_line и invalid count; неизвестная schema version не парсится как v1.
  Docker timestamp prefix допускается как known adapter, не regex-dump JSON recovery.
- [ ] Детектировать missing start/finish, duplicate event IDs, non-monotonic source
  clocks, truncation/drop markers и deferred links. Stable ordering timestamp +
  source order; не утверждать причинность только из порядка времён разных hosts.
- [ ] Никаких raw data до selection и финального sanitization в output. Linked jobs
  только того же incident; не брать все запросы того же user по умолчанию.
- [ ] Export default псевдонимизирует actor IDs и скрывает content, но сохраняет
  обязательный `key_suffix` для request/error evidence; по явным include flags
  применяет остальную spec policy. Никогда не отменяет credential scrubbing.
  Manifest содержит sensitivity/redaction/version/checksums/source window.
- [ ] Output directory новый, запрет symlink/path traversal/overwrite существующего
  incident; private permissions; plain Markdown escaping для недоверенного текста.
  Agent instructions явно отделены от log data.
- [ ] Пример передачи уже сохранённого synthetic input, не live access:

```bash
uv run --locked python scripts/log_incident.py --input synthetic.ndjson --request-id aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --output incident-synthetic
```

- [ ] Event catalog для каждого event: owner, level, required fields, outcomes,
  cause/retry semantics, privacy class, retention и synthetic example. Link source
  modules; retired events помечать совместимостью/schema version.
- [ ] Runbook: как искать «нет ответа», «не та модель», «долго», «после deploy»,
  «память не сохранилась»; отдельно failure vs unknown. Не обещать автоматического
  root cause: формировать evidence + candidate cause с confidence/unknown.

**Tests:** golden bundle для 12 сценариев spec, malicious preview instructions,
newline/Markdown/URL injection, invalid JSON, huge line, old schema, no data,
redaction at each include flag, conflicting output path, no network/import config.

## Задача 13. Сквозная приёмка, performance и staged rollout

**Files:** создать `tests/observability/test_incident_scenarios.py`,
`test_behavior_neutrality.py`, `scripts/benchmark_logging.py`; дополнить runbook.

**Consumes:** задачи 1–12.
**Produces:** проверяемый baseline и инструкция rollout/rollback без live claims.

- [ ] Пройти все 12 сценариев §11 spec, в том числе stream partial, race, Reader
  fallback, durable restart, pool wait, sink failure и синтетические secrets.
- [ ] Property/invariant: для нормального завершения ровно один terminal per
  request/attempt/delivery/job execution; при process kill missing terminal — unknown.
- [ ] Behavior-neutrality: на тех же mocked inputs не изменились provider/Telegram
  RPC arguments/count/order, dispositions, retry delays, DB transactions и keyboards.
  Исключение — намеренные observability format/ID changes, описать отдельно.
- [ ] Benchmark script использует только synthetic payload и fake sink: INFO normal,
  exception-heavy, concurrent 1000 events/sec, blocked sink и max-size events.
  Измерять producer p50/p95/p99, event loop lag, CPU/RSS/bytes/event и drops.
  Target для reference machine: normal producer p95 ≤1ms, no drops при согласованной
  normal load; throughput regression ≤5% относительно baseline business workload.
  Это приёмочные цели, не заранее доказанные свойства.
- [ ] Не увеличивать max bytes/depth бесконтрольно ради прохождения теста. Если target
  не достигнут — найти cost (serialization/stack/scrub/source walk), сохранить privacy.
- [ ] Запустить locked gates:

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy app bot.py
uv run --locked pytest tests/ --ignore=tests/integration -m "not integration" --override-ini="addopts=" --timeout=30
python -X utf8 scripts/check_encoding.py
git diff --check
```

- [ ] Integration DB/Redis только на явно isolated services, без production DSN;
  skipped/inaccessible — ограничение, не PASS. Offline container smoke отдельно.
- [ ] Staged rollout после отдельного разрешения: formatter/redaction+context →
  attempts/delivery/jobs → complete coverage/metrics → incident export/storage.
  Не включать previews раньше защиты CI/alerts/retention и approved policy.
- [ ] Canary: измерить event volume/lag/loss и trace completeness; rollback image
  должен читать новые optional job context fields, старые payload поддерживаются.
  Не dual-log одни события бесконечно и не откатывать scrubbing включением raw DEBUG.
- [ ] Exit checklist: контракты/сценарии проверены, release searchable, exceptions
  usable, no raw secret, latency bounded, retention проверен на выделенной среде,
  agent bundle читаем, все оставшиеся live limitations явно записаны.

## Задача 14. Опциональная инфраструктура после baseline

Не блокирует исправление L01–L12. Не устанавливать/деплоить автоматически только
потому, что пользователь разрешил рассмотреть новые зависимости.

### 14A. Alloy + Loki + Grafana

- Выделенный infra change: pinned image versions, storage capacity, retention,
  auth/TLS, backup, resource limits, independent availability monitoring.
- Collector парсит один NDJSON envelope, сохраняет high-cardinality fields как
  structured metadata; не дублирует body и не переносит previews в long-retention stream.
- Saved views: request timeline, provider/key failures, partial/delivery issues,
  deferred jobs, dependency fallback, startup/shutdown, logging losses.
- Приёмка: запрос до и после redeploy находится по request_id; collector outage
  восстановлен в пределах измеренного spool; reader не имеет credentials администратора.
- До deployment прочитать текущую документацию выбранных версий. Не включать публичный
  Loki/Grafana endpoint, не предоставлять бот-процессу Docker socket.

### 14B. OTel traces

- Рассмотреть `opentelemetry-api`, `opentelemetry-sdk`, OTLP exporter; зафиксировать
  проверенные версии под Python 3.14 через uv, изменяя manifest/lock только в этой задаче.
- Manual spans у ingress/provider/delivery/jobs; sampling успешных traces с сохранением
  важной ошибки в logs независимо от sampled flag; не отправлять payload/secret attrs.
- Сопоставить trace_id/span_id с логами, проверить actual traceparent propagation
  только в поддержанных trusted boundaries; external arbitrary client ID не parent.
- Не подключать duplicate auto logging handlers. OTel exporter outage не блокирует bot.
- Приёмка: trace с provider retry и deferred link совпадает с NDJSON incident;
  отключение OTel оставляет весь baseline работоспособным.

## Финальная передача агентом-исполнителем

Предоставить:

1. Что реализовано по tasks/findings, что осталось и почему.
2. Ссылки на event catalog/runbook и **синтетический** incident bundle.
3. Реальные команды/результаты тестов, benchmark и artifact verification.
4. Список новых settings и их безопасные defaults, изменения dependencies.
5. Known compatibility/operational limitations и отдельные запросы на live actions.

Не утверждать, что система «ловит любую root cause», что unit tests доказывают
production completeness или что пароль делает safe любое содержимое логов.

## Обязательный остаток после независимого ревью реализации

До production-complete следующий агент должен закрыть и отдельно проверить:

- вторичные Telegram/PostgreSQL error sink'и через единый sanitized incident
  summary вместо raw message/traceback;
- bounded conversion неизвестных `extra` без вызова `str()`/пользовательского
  кода и без обходного `handleError(record)`;
- Telegram Bot API token/path fallback-pattern плюс раннюю регистрацию bot token;
- edited-message content policy без безусловного 80-символьного INFO preview;
- один правдивый provider attempt terminal после проверки empty completion;
- terminal local-validation для OpenRouter/Opencode до HTTP attempt;
- отдельный authenticated WebSocket request/user/trace scope с cleanup;
- bounded correlation closure incident export по связанным trace/task/attempt/error
  IDs, чтобы selector не обрезал причинную цепочку;
- recovery/loss summary после временного отказа всех sink'ов;
- соответствие queue event catalog: `job.enqueued`, `job.capacity_rejected`,
  `job.retry_scheduled` и ровно один owner terminal на execution.

Для каждого пункта сначала добавить failing regression на фактический producer
contract, затем минимальное исправление и профильный тест. Эти пункты не являются
разрешением на новый collector, dependency, DB migration или deploy.

## Self-review плана

- Findings L01–L03: задачи 1, 2, 5; L04–L06: 3, 7; L07–L08: 5, 6.
- L09–L10: 2, 11, 12; L11: 10; L12: 4; L13: 11/14A; L14: 5/7/9/10;
  L15: 8/9; L16: все contract tests и задача 13.
- Все области spec §8 имеют owner/task; scripts и специализированные SDK paths
  не считаются автоматически покрытыми chat router.
- Обязательный baseline без новой зависимости; нет скрытой DB migration или deploy.
- Sensitive-content choices являются явной policy, metadata default; работа
  над планом не означает согласия пользователей на дополнительные формы хранения.
