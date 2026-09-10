# Аудит логирования и проект целевой системы

Дата: 2026-09-10. Проверенный исходный checkout: `fb8f6166668a673500946a76b665820d27d9fab8`.
Статус: разделы 1–12 фиксируют baseline-аудит и проектное решение на указанном
checkout. Текущая ветка содержит baseline-реализацию; её фактический контракт и
runbook находятся в [logging.md](logging.md) и [log-events.md](log-events.md).
Практический план: [logging-overhaul](superpowers/plans/2026-09-10-logging-overhaul.md).

## 1. Главный вывод

Проблема не в недостаточном количестве строк. В проекте много диагностических
сообщений, но они не образуют надёжную историю выполнения. Сейчас трудно ответить:
«какой запрос сломался, на каком этапе, почему, какие попытки были сделаны и что
в итоге увидел пользователь».

Самый важный подтверждённый дефект: при текущей настройке JSON обычный stdlib
logger теряет время, уровень, имя модуля, correlation context и `extra`.
Исключение с `exc_info=True` превращается в строковое представление traceback
object, а не в пригодный для расследования стек. Это важнее установки нового UI.

Рекомендация: сохранить stdlib + установленный structlog, исправить общий
конвейер, ввести стабильные события и контекст, связать ingress, provider attempts,
delivery и jobs. Затем обеспечить хранение между деплоями, поиск и incident bundle
для человека/агента. Логи должны быть достаточно полными для проверки гипотез,
а не максимально многословными.

Ни один лог не гарантирует root cause **любых** проблем: SIGKILL/OOM, потеря диска,
неизвестная внутренняя ошибка провайдера и неполные данные требуют внешних событий,
метрик и иногда воспроизведения. Не называть последнюю видимую ошибку первопричиной
без подтверждения причинной цепочки.

## 2. Границы и метод проверки

Выполнено:

- Инвентаризация всех 239 Python-файлов `app/**/*.py` и `bot.py` через AST;
  поиск логирующих вызовов, exception handlers, контекста и конфигурации.
- Детальное чтение центрального logging pipeline, API logger, tracing, ingress,
  update processor, decorators, provider router и stream adapters, delivery,
  очередей, metrics/exporter, alerts, retries, deployment logging.
- Просмотр call sites во всех подсистемах, особенно содержимого сообщений,
  ключей, URL, ошибок и скрытых fallback.
- Проверка манифеста, README, архитектуры, CONTEXT, CONTRIBUTING, документации
  и применимых AGENTS.md. В `app`/`tests` дополнительных AGENTS.md не найдено.
- Локальные синтетические проверки фактического stdout и исключений.
- 26 focused tests: контекст, API logger, исходящие request headers, decorators.
- Сверка предлагаемых механизмов с официальной документацией; ссылки ниже.

Не выполнялось: чтение `.env`, реальных сообщений/секретов, закрытых production
логов, подключение к VPS/Telegram/provider APIs/DB, запуск бота, миграции,
изменение инфраструктуры. Не установлены реальные log driver, retention, ACL,
заполнение диска, интенсивность событий и настройки внешнего сборщика на VPS.
Наличие парольной защиты принимается со слов владельца, а не как проверенный факт.

### Размер области

AST-счётчик распознавал вызовы `logging/logger/log/_logging/self.logger` и
выражения с `getLogger`, уровни debug/info/warning/error/exception/critical.
Это воспроизводимая инвентаризация типовых call sites, не доказательство
семантической корректности каждой строки и не измерение production throughput.

| Область | Python-файлов | Logging calls |
| --- | ---: | ---: |
| bot.py | 1 | 112 |
| Корневые модули app | 44 | 382 |
| handlers | 50 | 362 |
| providers | 16 | 149 |
| response_delivery | 7 | 9 |
| repos | 30 | 162 |
| games | 21 | 149 |
| utils | 29 | 88 |
| context | 5 | 15 |
| core | 2 | 26 |
| db | 6 | 18 |
| documents | 4 | 15 |
| natal | 21 | 6 |
| middleware | 2 | 9 |
| adapters | 1 | 3 |
| **Итого** | **239** | **1505** |

Логирование есть в 151 файле. Из распознанных вызовов: 419 INFO, 497 WARNING,
380 ERROR, 12 CRITICAL, 190 DEBUG, 7 exception. 937 вызовов идут прямо через
корневой `logging`, а не через именованный модульный logger.
166 ERROR/CRITICAL не передают `exc_info`; это список кандидатов для классификации,
а не 166 доказанных ошибок: событию без исключения стек не нужен.
Найдено 1177 exception handlers, включая 105 с единственным `pass`.
Ожидаемые timeout/cancel/best-effort случаи не следует автоматически превращать
в ERROR. Нужно классифицировать причины и последствия.

## 3. Что уже полезно и должно сохраниться

- `app/request_context.py`: asyncio ContextVars с request/user/chat IDs;
  имеются тесты изоляции корутин и наследования задачами.
- `app/tracing.py`: scope с восстановлением trace/span через tokens.
- `TaskManager`: отслеживание задач, retries, захват context snapshot, bounded drain,
  error callback. Это полезная основа, не «полностью отсутствующее управление jobs».
- `app/providers/stream_types.py`: typed terminal events, error codes, failure phase,
  retry/key dispositions, route, usage, grounding. Не выводить эти факты из текста
  ошибок заново, когда они уже существуют в типизированном контракте.
- `app/response_delivery/outcomes.py` и `renderer.py`: immutable outcome/receipt;
  есть фактический способ доставки и ссылки на Telegram messages.
- Circuit breaker логирует переходы состояний; TTS имеет job_id и queue_wait_ms;
  авторинг daily games уже содержит отдельные диагностические события.
- `httpx/httpcore` ограничены WARNING+, что уменьшает обычный HTTP-шум и вывод
  Telegram URL с токеном. Это не замена очистке ошибок на всех уровнях.
- PostgreSQL metrics/error_logs, dashboard и rate-limited admin alerts существуют.
  Они не заменяют полноценное хранилище логов, но их нужно связать с ним.

## 4. Findings: подтверждённые дефекты и пробелы

Приоритеты: P0 — сначала, так как теряется основное доказательство; P1 — core
диагностируемость; P2 — полнота/эксплуатационная зрелость. Это не CVSS.
Номера строк относятся к проверенному checkout; перед изменениями искать символы.

### L01 · P0 · Общий pipeline не обрабатывает stdlib записи

Источник: `app/utils/logging_config.py:54–75`, `:83–102`, `:174–206`.
`ProcessorFormatter` получает только renderer, без `foreign_pre_chain`.
`RequestContextFilter` определён, но setup не прикрепляет его к handlers.
Нет `ExtraAdder`. Shared processors работают для structlog, а подавляющее
большинство приложения использует stdlib.

Синтетическое воспроизведение на установленной среде:

```python
setup_detailed_logging(enable_structured_logging=True)
set_request_id("audit-synthetic")
set_user_context(42, 84)
logging.getLogger("audit.stdlib").info("stdlib probe", extra={"operation": "probe"})
```

Фактический stdout: `{"event": "stdlib probe"}`.
Вызов `get_logger("audit.structlog").info(...)` в том же процессе содержит
request/user/chat, timestamp, service, hostname, level, logger.
Итак, JSON валиден синтаксически, но диагностически неполон.

### L02 · P0 · Exception logging теряет пригодный стек

Тот же pipeline: `format_exc_info` отсутствует в пути foreign records.
Контролируемый `raise RuntimeError("SYNTHETIC only")` + `logging.exception(...)`
выдал `exc_info` с текстами класса, исключения и `<traceback object at ...>`.
Файлов, функций и строк traceback в выводе нет.
Кроме того, `APILogger.log_error` (`api_logger.py:130`) использует
`traceback.format_exc()`, а не traceback переданного `error`: вне активного except
может получить `NoneType: None`. У существующего unit test именно такой вызов,
но содержимое traceback он не проверяет.

### L03 · P1 · API JSON спрятан в message, а начало/конец непарные

Источник: `app/utils/api_logger.py:44–152`.
Сначала JSON сериализуется в строку, затем обрамляется emoji и `REQUEST STARTED`,
затем outer renderer сериализует всё ещё раз. Для фильтрации полей нужен второй
парсер. Внутри используется локальный naive datetime и `time.time()` для duration.
`LOG_PRETTY` читается singleton при импорте независимо от финального JSON-mode.
`**fields` могут переопределить context/status/duration.

`messages.py:185` открывает API-событие с api=telegram для всего handler, а не для
конкретного Telegram RPC. После него есть ранние return, например слишком длинное
сообщение/rate limit. OpenRouter legacy пишет response, но не такой же generic
request. Слово «API» сегодня объединяет разные единицы измерения.

### L04 · P1 · Контекст задаётся слишком поздно и не на всех входах

Источники: `messages.py:153–166`, `utils/decorators.py:12–25,85–96`,
`update_processor.py:36`, `web.py:93`, `handlers/inline.py:519`.
В message handler state hydration происходит **до** set_request_id.
Контекст вручную задаётся в части callbacks/decorators; нет единого owner для всех
updates, edited messages, inline/chosen results, отказов, web/WS и scheduled jobs.
Setters не возвращают reset tokens, decorators не восстанавливают предыдущий
контекст. Это риск неверного attribution на повторно используемом task;
между независимыми asyncio tasks уже есть изоляция, её нельзя объявлять сломанной.

Request IDs включают chat/user IDs (`tgmsg-...`, `tgcb-...`), а часть HTTP adapters
передаёт их наружу в X-Request-ID. Лучше разделить opaque correlation ID и actor IDs.

### L05 · P1 · Tracing существует, но не подключён к выходным логам

Источник: `app/tracing.py:12–43`; единственный application binding — messages.py.
`get_trace_context()` не используется logging pipeline. Формат span_id содержит
имя и 8 hex; это не OTel/W3C span ID. Нельзя называть текущий механизм полноценным
distributed tracing или передавать эти значения как traceparent.

### L06 · P1 · Durable queue обрывает причинную связь и смешивает результаты

Источники: `app/queue.py:29–100,408–422`, `app/deferred_response.py:20–47,128–136`.
Task JSON сохраняет task_id/user_id/data, но не origin request/trace/parent span.
Worker создан на startup и не получает context каждого задания автоматически.
`TaskManager.copy_context()` не решает межпроцессную/Redis-передачу.

Отдельный подтверждённый semantic mismatch: deferred handler возвращает
`{"status": "failed", ...}` при неудачной доставке. Queue считает любое нормальное
возвращение COMPLETED и пишет `Task ... completed successfully`.
Для логов нужно разделить `execution_outcome=returned` и `business_outcome=failed`.
Автоматическая смена ack/retry semantics — отдельное изменение поведения,
не обязательная часть logging refactor.

### L07 · P1 · Критический streaming путь беднее legacy API logging

Источники: `providers/gemini.py:302`, `openrouter.py:stream`,
`opencode.py:stream`, `router.py:716–1109`.
Legacy `_execute_request` вызывает APILogger и metrics; новые stream methods
в основном возвращают typed events без сопоставимого attempt lifecycle.
Router выводит выбранную модель, prefixes key_hash и round; нет единого
attempt_id, provider response request ID, измерений first token/stream duration,
race winner/loser outcomes и summary fallback chain.

Stream exceptions превращаются в `StreamFailed.diagnostic`. Обрезанная строка
не сохраняет traceback; Gemini сначала режет до 500 символов, потом заменяет
полный api_key, поэтому срезанный ключ может не совпасть. HTTP-error bodies в
OpenRouter/Opencode используют отдельный путь. Нет единой политики для всех веток.

### L08 · P1 · Генерация и доставка не имеют общего terminal summary

Источники: `response_delivery/delivery.py:75,103`, `coordinator.py:run`,
`renderer.py:248–454`, `outcomes.py`.
Существуют богатые outcomes/receipts, но facade не пишет terminal event.
Есть отдельные warning про Reader/Telegraph/edit fallback; итоговая цепочка
не видна как один контракт. Успешный provider ответ не доказывает доставку.
`FailedDelivery` может означать, что пользователь успешно получил сообщение
об ошибке; это тоже не delivery transport failure.

### L09 · P1 · Нет единой политики sensitive data

Фрагменты сообщений уже есть: `messages.py:198,697`, `inline.py:519,1116`,
`msg_voice.py:162`. Полный пользовательский role description:
`msg_roles.py:269`; полный ответ при parse failure: `:325`, `cb_roles.py:318`.
Research пишет URL и recall query (`core/agentic.py:422,463`), cache пишет query
prefix (`cache.py:154,164`) и отдельные inline tokens (`:458–477`).
OpenRouter legacy печатает error body (`openrouter.py:558`), Opencode response
object (`opencode.py:557`). Exceptions могут содержать URL, body и credentials.

`router.py:173` берёт первые 8 символов сырого ключа, а другие места берут 8
символов key_hash: это разные идентификаторы. Некоторые prefixes провайдеров
одинаковы и плохо помогают различать ключи.
`config.py:803–807` generic debug/testing setter логирует любое значение settings;
это опасная возможность, но активного production caller в checkout не найдено.

### L10 · P1 · Логи покидают «закрытое место»

`.github/workflows/deploy.yml:314,352,428,449` выводит tail контейнеров в CI.
`admin_alerts.py:94–110` отправляет exception traceback в Telegram;
`metrics.py:267–277` сохраняет error_message в PostgreSQL, dashboard отдаёт его.
Защита одного VPS каталога не покрывает эти копии. GitHub masking известных
секретов не является универсальным scrubber для BYOK, персональных данных,
частичных токенов и строк, которые провайдер отразил в ошибке.

### L11 · P1 · Метрики не отражают все ошибки и exporter читает не те поля

Источник: `prometheus.py:24–46`, `metrics.py:31–48,502–513`.
Exporter читает `_start_time`, `_api_calls`, `_errors`; collector их не задаёт,
других app-assignments при поиске не найдено. Uptime получается 0, API/error
series отсутствуют, хотя metadata HELP/TYPE присутствует.
Основные счётчики находятся в `collector.metrics`; provider/model распределения
не равны существующему `_api_calls[(provider,model)]` — нельзя выдумать пары из
двух независимых marginal counters.

`logging.error` сам не вызывает `record_error`; dashboard показывает лишь ручные
вызовы последнего. In-memory error_log ограничен 100, API events — 200;
сохранение раз в 300 секунд не является надёжным error archive.
`_events_queue=asyncio.Queue()` не ограничена. При быстром накоплении или проблемах
consumer возможно давление на память. Это отдельная очередь, не logging queue.

### L12 · P1 · Sink может блокировать event loop, а сбои sink скрываются

Источник: `logging_config.py:174–194`.
StreamHandler пишет синхронно; FileHandler тоже, без rotation. Ошибка создания
файла проглатывается. Named api_logger/telegram/asyncpg/httpx/httpcore имеют
собственные handlers и `propagate=False`: root file handler не получает их записи.
Повторный setup не обновляет уже существующие named handlers. Root handlers
снимаются без явного close. В штатном startup file sink не включён, поэтому
это дефекты опционального API, а не доказанный текущий рост `/tmp/bot_detailed.log`.

### L13 · P1 · Хранение и release attribution не заданы в репозитории

Deploy создаёт tg-bot/tg-api/tg-media-cleanup без явного logging driver/rotation.
Это **не доказывает**, что daemon-level rotation отсутствует на VPS.
Старые контейнеры удаляются; без независимого retention их локальная история
не является долговечным источником расследований. App logs не имеют обязательных
release SHA, process instance и build/config revision.

### L14 · P2 · Levels и ошибки не имеют общего семантического контракта

`background_tasks.py:142` пишет ERROR на каждую retry attempt;
`resilience_policy.py:run_with_resilience` вовсе не пишет решения о retry/delay.
Часть ошибок подавляется, часть дублируется provider → metrics → handler → alert.
`timed_operation` фиксирует failure на DEBUG и не используется как общий механизм.
`bot.py:136–163` может после `Database unavailable` написать
`All systems operational`: текст heartbeat не является доказательством здоровья.
Константные health-сообщения могут скрывать значимые изменения в потоке INFO.

### L15 · P2 · DB/cache/locks недостаточно объясняют задержки и деградацию

Источники: `database.py:230–305`, `adapters/concurrency.py:89`, `state.py:196–252`,
`context/compression.py`, `repos/memory*.py`, `cache.py`.
Есть ошибки и часть counters, но нет единого split: queue_wait, lock_wait,
pool_acquire, query_duration, provider latency, rendering, Telegram RPC.
Простой end-to-end timeout не говорит, где потрачен бюджет.
Не все SQL идут через db_query: memory graph writer работает на caller connection;
глобальный wrapper над db_query один не покрывает эти транзакции.

### L16 · P2 · Проверки подтверждают helpers, но не качество фактических логов

`test_api_logger_request_id.py` анализирует аргументы MagicMock, а не stdout;
`test_request_context.py` вручную вызывает filter. Есть полезный тест подавления
httpx DEBUG в `test_audit_fixes.py`. Нет общего schema/traceback/redaction/rotation/
queue-pressure/sink-failure/incident-reconstruction контракта.
26 проверенных тестов прошли, несмотря на L01/L02.

## 5. Целевая архитектура: выбранный вариант

### Варианты

| Вариант | Выгода | Цена/ограничение | Решение |
| --- | --- | --- | --- |
| Исправить stdlib + structlog, NDJSON, bounded writer, incident CLI | Максимально использует текущий код; быстрый эффект; локально проверяется | Нужно ввести event discipline и retention | **Обязательная основа** |
| Основа + Alloy/Loki/Grafana | Общий поиск между контейнерами/деплоями, dashboards и alerts | Отдельные ресурсы, ACL, backup, эксплуатация | Рекомендуемая последующая опция после замеров VPS |
| Полный OTel stack + trace/error backend с первого этапа | Distributed traces и service maps | Больше изменений, SDK/exporters и риск дублей; не исправит плохие исходные события | Отложить до стабильного schema |

Не добавлять Loguru/python-json-logger параллельно имеющемуся structlog ради
ещё одного формата. Не строить новую logging database внутри основной PostgreSQL:
отказ БД не должен лишать диагностики её же отказа.

Официальный structlog описывает `foreign_pre_chain` для stdlib, `ExtraAdder`
для extra и общий ProcessorFormatter. Это подтверждает выбранный способ
совместимости, не необходимость массово переписать все callers сразу.
[Источник](https://www.structlog.org/en/stable/standard-library.html).

### Поток данных

1. Ingress создаёт доверенный context до auth/state/locks; только явно проверенные
   user/chat IDs попадают в actor context. Непроверенные claims остаются claims.
2. Domain boundary пишет стабильное событие и измерения; exception owner сохраняет
   тип/stack/cause до превращения ошибки в typed outcome или user-friendly текст.
3. В producer context строится bounded, очищенный, JSON-compatible snapshot.
4. Одна bounded in-process очередь → отдельный writer thread → NDJSON stdout.
   Ни DB, ни HTTP, ни Telegram внутри log handler.
5. Docker retention + при включении независимый collector/store.
6. Read-only incident CLI экспортирует выбранную историю и указания на пробелы.

ContextVars нельзя читать только в listener thread: там будет чужой/пустой context.
Стандартный QueueHandler.prepare меняет msg/args и очищает exc_info; собственный
snapshot должен сохранить нужные безопасные данные **до** этого шага.
[Python logging handlers](https://docs.python.org/3.14/library/logging.handlers.html).

## 6. Контракт события v1

NDJSON UTF-8: одна физическая строка = один объект, без ANSI/emoji-префиксов.
Человеческое описание может быть RU; имена событий, полей, enum — English snake_case.
`event` — стабильное имя вроде `provider.attempt_finished`, не динамический текст.
`message` — пояснение; ID/ошибки/тайминги не нужно извлекать regex из message.
Не мигрировавший caller временно получает `event=legacy.log`, `message` и envelope.

| Поля | Правило |
| --- | --- |
| schema_version, timestamp, level, event, message | schema_version=1; UTC RFC3339 с Z; level lowercase |
| event_id, service, environment, release, instance_id | event_id=32 hex; release=точный SHA или `unknown`; instance_id новый при старте процесса |
| logger, source | module; source={file,function,line}, относительно repo, без locals |
| request_id, trace_id, span_id, parent_span_id | nullable для startup/system; opaque IDs; request/trace 32 hex, span 16 hex |
| execution_id, task_id, attempt_id | execution_id для исполнения durable task; attempt_id для реальной внешней попытки |
| user_id, chat_id, update_id, message_id | отдельные поля; не зашивать в request_id; IDs не считать credentials |
| operation, outcome, reason_code, error_id | точная операция, факт результата и машинная причина |
| duration_ms, queue_wait_ms, lock_wait_ms, first_token_ms | monotonic durations; отсутствующее измерение = null/отсутствует, не 0 |
| exception | очищенный type/message/stack/causes/group members, без locals и repr живых объектов |
| provider, requested_model, actual_model, key_fingerprint, key_suffix | реальные значения route; `key_suffix` обязателен для каждого запроса/ошибки с фактически выбранным ключом и содержит минимум последние 4 символа |
| http_status, provider_request_id, timeout_kind, retry_after_ms | только доступные значения; отсутствие upstream ID не выдумывать |
| attempt_no, max_attempts, retry_scope, retry_delay_ms, race_id | одна сеть-попытка != key round != model fallback |
| delivery_kind, delivery_status, generation_status | различать получение AI текста и показ ответа/сообщения об ошибке |
| redacted_fields, truncated_fields, content_policy | объяснять, что скрыто/усечено и по какой политике |

Envelope keys защищены: payload не может менять level/timestamp/request_id/event_id.
Только emitter/context owner создаёт их. Неизвестный JSON-compatible domain field
допустим лишь после allowlist-review соответствующего события.
Bounded defaults: event 32 KiB UTF-8, обычная строка 2 KiB, stack 12 KiB,
до 32 frames, до 8 cause/group members, collections до 32 items и 5 уровней.
Сначала clean/redact, потом truncate, потом повторная проверка размера.
Большие события сокращать по приоритетам; IDs/outcome/error type сохранять всегда.

`exception` не обязан содержать stack для ожидаемого 429 без программного сбоя.
Для неожиданного exception нужен реальный stack, exception chain и error_id.
Повторные упоминания ссылаются на error_id, не дублируют один traceback во всех слоях.
Fingerprint ошибки: тип + операция + нормализованные application frames + code;
не ID пользователя, не текст сообщения и не динамическая строка провайдера.

### Жизненный цикл и владельцы

- `request.started` / `request.finished`: один owner ingress; finished на все
  controlled exits, including rejected/duplicate/cancelled. После process kill
  отсутствие finished — факт незавершённости, не синтетический success.
- `provider.attempt_started` / `provider.attempt_finished`: provider boundary;
  winner/loser при race — отдельные attempts, loser cancellation не ERROR.
- `provider.retry_scheduled`, `provider.fallback_selected`, `provider.race_resolved`:
  router/resilience owner; причина и переход from/to. Не менять retry policy.
- `generation.finished`: router/consuming boundary фиксирует typed terminal,
  finish reason, route, usage, before/after_text. Один terminal для одной generation.
- `delivery.finished`: delivery facade после actual receipt, либо transport failure.
  `generation_status=failed` + `delivery_status=sent` допустимо: показали ошибку.
- `job.enqueued`, `job.started`, `job.finished`, `job.retry_scheduled`: queue/manager;
  origin request сохраняется; новый execution/span на каждую worker attempt.
- `dependency.state_changed`: breaker/DB/Redis/health переход и восстановление.
- `diagnostic.enabled/expired`, `logging.loss_summary`, `logging.sink_failed`:
  наблюдаемость самого механизма.

Для HTTP request и Telegram update из webhook это разные scopes: HTTP acceptance
не означает completion update. Передать correlation через явный bounded envelope
или ограниченное хранилище по update_id, не надеяться на контекст очереди PTB.
Для forwarded/media-group batch хранить список связанных update/message IDs с
лимитом; не терять причины поглощения сообщений debounce/dedup.

## 7. Ключи, IDs, секреты и фрагменты сообщений

Учитываем желание владельца видеть IDs и полезные части сообщений. Не требуется
обезличить всё до бесполезности. Но пароль к логам — не основание хранить произвольные
credentials. OWASP отдельно выделяет access tokens, passwords и encryption keys
как данные, которые не следует записывать напрямую.
[Рекомендации OWASP](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html).

Политика обновлена владельцем 2026-09-10 и реализована в protected operational
stream. Приоритет — воспроизводимый debugging; ограничения ниже по-прежнему
запрещают полные credentials и перенос message-bearing rows в alerts/CI/public
artifacts:

| Данные | Обычный operational log | Ограниченный diagnostic режим |
| --- | --- | --- |
| user_id/chat_id/update_id/message_id/job_id | Полностью в закрытом хранилище | Полностью; экспорт умеет стабильно псевдонимизировать |
| Provider key | Стабильный key_fingerprint, provider, key source, состояние лимита **и минимум последние 4 символа ключа** для каждого запроса с ключом и каждой ошибки ключа | Те же обязательные последние 4 символа; scoped diagnostic может добавлять контекст попытки, но не раскрывает полный ключ |
| ADMIN_SECRET, password, bot token, DB/Redis credentials, cookies, initData, JWT, service-account JSON | Никогда, включая части | Никогда |
| Обычное текстовое сообщение авторизованного пользователя, включая group chat | Очищенный bounded текст до 2048 Unicode chars + длина/fingerprint | `preview` оставлен как scoped 256-char compatibility mode |
| Команды добавления ключей, auth/initData payload, birth/natal data, LTM contents, документы | Только тип, длина, IDs/счётчики | Не открывать автоматически этим режимом |
| URL/HTTP headers/body | method, allowlisted host/route, status, lengths | Разрешённые структурированные поля; query/userinfo/token path вырезать, body preview только явно разрешённого error schema |
| Provider output в явно инструментированных путях | Очищенный bounded текст до 2048 chars + length/tokens/finish reason/validation code | Scoped preview при явном `preview` mode |
| Изображения, voice/audio bytes, attachments | mime, bytes, duration, dimensions, IDs | Никаких base64/bytes |

Текущий production default — `LOG_CONTENT_MODE=full`; `metadata` является явным
операторским rollback для отключения текста. Доступ к operational stream должен
оставаться ограниченным, а Docker rotation — включённой. LTM consent не даёт
разрешение логировать private-memory contents; natal/document/auth data также
остаются metadata-only.

Fingerprint: использовать уже имеющийся необратимый `key_hash`, с provider namespace
и единым форматированием; если у specialized key нет такого hash — helper с тем же
алгоритмом идентификации и без раскрытия сырого ключа. Поле `key_suffix` обязательно
содержит последние 4 Unicode code points непустого ключа (или весь ключ, если он
короче четырёх символов) во всех событиях запроса/ошибки, где ключ фактически выбран.
Для correlation хранить достаточную длину fingerprint (например 16 hex), а не только
общий текстовый prefix API-key. Никогда не подменять `key_suffix` концом `key_hash`.
Не применять ADMIN_SECRET как новый tracing secret; не менять шифрование ключей.

Редакция в два слоя: типизированные allowlisted поля в call sites + общий scrubber
в pipeline/exports/alerts. Pattern и exact-secret replacement — страховка, не
обещание выявить любые персональные данные в свободном тексте. Не делать raw prompt
доступным при включении DEBUG. Не выводить headers целиком даже на TRACE.
Для неизвестного ключа `key_suffix=null` и `key_present=false`; для выбранного ключа
suffix обязателен даже вне diagnostic режима. Это осознанное исключение из общей
политики сокрытия credentials по прямому требованию владельца. Scrubber должен
разрешать только типизированное поле `key_suffix`, но продолжать вырезать тот же
фрагмент, если он случайно встретился в message/exception/body/URL.

Диагностика: operator задаёт incident_id, target request или user, subsystem,
UTC deadline ≤15 минут; при истечении автоматический возврат к обычной политике.
Все заданные selectors применяются через AND. Нельзя включить широкую диагностику
одной переменной `LOG_LEVEL=DEBUG`. Activation/expiry логируются без содержимого.
Runtime ограничивает Docker logs ротацией `20m × 5` на контейнер; это фактический
bounded retention по объёму, но не архивная гарантия по времени. Message-bearing
rows нельзя копировать в CI artifacts/Telegram alerts. Для внешнего collector
потребуется отдельное решение о сроке, access control и удалении.

## 8. Покрытие подсистем: какие факты нужны

| Область | Источники | Что добавить, не меняя бизнес-логику |
| --- | --- | --- |
| Telegram | bot.py, update_processor, messages, inline, decorators, middleware | Вид update/handler, admission, auth/rate/dedup reason, queue/user/global wait, batch links, terminal outcome |
| Web/WS | web.py, web_miniapp.py, web_reader.py, web_natal.py | route template, method/status, auth outcome, generated request ID; WS session/turn IDs, close code, direction counters, idle/timeout phase; без initData и audio frames |
| Chat routing | providers/router.py, request_factory.py, base.py, resilience_policy.py | requested/actual route, key fingerprint/source, rounds/attempts/races, retry policy decision, TTFT, usage/finish, cancellation cause |
| Specialized APIs | imagen_provider, pollinations, freetheai_image/audio, tts, elevenlabs_tts, games/daily_ai | Та же attempt schema; собственные boundary adapters, не перенаправлять через chat router |
| Search/research | search_services, search_jina, core/agentic.py, intent_router | tool name/call ID, iteration, safe host, cache decision, result count, budget, termination reason, parse error code |
| Delivery | response_delivery/*, utils/messaging.py | transport operation/status, edit/send recovery, split parts, reader storage/display, opt-in publication skipped, immutable receipt summary |
| Queue/background | queue.py, deferred_response, background_tasks, voice_engine | origin/execution/job IDs, age/wait/runtime, retry/exhaustion/recovered after crash, business outcome separately, drain/cancel |
| Scheduled/broadcast/games | bot jobs, scheduled_briefs/horoscopes, cmd_reminders, daily handlers, games, web.py | run/batch/delivery ID, due time/lag, selected/sent/skipped/failed counts, per-item failure; progress не каждую запись |
| Database/migrations | database.py, db/*, repos/*, scripts/migrate.py | operation name, pool wait, execution/transaction duration, SQLSTATE/retry, migration version/checksum, rollback result; без SQL parameters |
| State/Redis/cache | state.py, cache.py, adapters/concurrency | load/persist/superseded, local vs Redis, timeout/fallback, cache namespace/hit/miss, lock wait, dirty persistence marker без содержимого |
| Context/LTM/docs | context/*, document_processor, documents/*, repos/memory* | бюджет/число частей, consent epoch, source IDs/counts, lease/write outcome, rollback, extracted counts, parser stage; без фактов памяти/текста файла |
| Natal/voice/media | natal/*, voice_engine, multimodal_processor, utils/audio/image | stage/format/model, resource duration/size, parse/render failure; без birth values, текстовых транскриптов по умолчанию |
| Ops/health | bot.py, memory_manager, degradation, circuit_breaker | release/build/start mode, safe config summary, dependency state changes, process uptime/resource pressure, shutdown reason и drain results |

## 9. Уровни, объём и эксплуатация

- INFO: meaningful lifecycle summaries, финалы обычных запросов и изменения
  конфигурации/состояния. Не каждый stream chunk, SQL или cache hit.
- WARNING: recoverable failure/retry/fallback, partial response, необычная задержка.
  Не повторять одинаковый dependency outage без aggregation.
- ERROR: terminal operation failure или неожиданный exception, требующий анализа.
- CRITICAL: процесс не может безопасно стартовать/продолжить, нарушен обязательный
  startup invariant. Пользовательский 400/отмена не CRITICAL.
- DEBUG: bounded stage detail и scoped диагностика; не разрешение на raw secrets.

Не sample terminal failures, meaningful state transitions и terminal summaries
в штатной нагрузке. Можно aggregate progress/cache events; sampling начала/деталей
успешных requests должно быть consistent per request, с указанием sampling policy.
Очередь ограничить 4096 events и 8 MiB суммарно, max event 32 KiB. При перегрузке
отбрасывать DEBUG/INFO до WARNING/ERROR, зарезервировать ёмкость для важных событий.
Даже критичные события могут потеряться при полном отказе sink: счётчики потерь,
rate-limited emergency stderr, health degradation и честная отметка в incident bundle.
Не обещать simultaneously zero loss, nonblocking и bounded memory при disk failure.

Идемпотентный setup, один owner handlers, безопасный stop/drain ≤3 секунд.
Ошибка file sink/config должна быть видимой. В контейнере stdout — основной sink;
не дублировать каждый event в растущий файл внутри контейнера.

Для минимальной инфраструктуры зафиксировать Docker `local` с 20m × 5 или
`json-file` с явными лимитами при необходимости конкретного collector. `local`
поддерживает rotation; daemon defaults не меняют уже созданные контейнеры.
[Docker logging](https://docs.docker.com/engine/logging/configure/).

Предел в мегабайтах не гарантирует 14 дней: retention рассчитывается из measured
events/sec × avg bytes × days с резервом. При collector downtime проверить
доступное окно spool/rotation; история должна сохраняться и после удаления контейнера.

Alloy умеет читать Docker logs. Доступ collector к Docker API — привилегированная
граница: отдельный collector, минимальные права/изоляция и отсутствие публичного
socket; нельзя считать read-only mount socket полноценным read-only API.
[Alloy Docker source](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.docker/).

В Loki labels только service/environment/level и небольшой фиксированный subsystem;
request_id/user_id/trace_id/task_id/key fingerprint — JSON fields/structured metadata,
не index labels. Иначе cardinality взрывается.
[Loki labels](https://grafana.com/docs/loki/latest/get-started/labels/).

OTel API/SDK + OTLP exporter добавлять отдельной опцией после baseline.
Не называть локальные span records распределённым tracing до реальной propagation
и exporter verification. Не подключать две logging integrations одновременно.
[Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/).

## 10. Что должен получать человек и агент

Read-only CLI `scripts/log_incident.py` (планируемый) принимает NDJSON/plain Docker
stdout stream, request_id/error_id либо user_id + обязательное UTC окно времени.
Для Docker источника использовать `docker logs` API/CLI, не читать его внутренние
файлы хранения как публичный формат. Не брать `.env`, SQL dump и соседние чаты.

Выход: `events.ndjson`, `incident.md`, `manifest.json`:

- SHA/release, time window, request/trace/job/attempt IDs и режим scrubber;
- timeline со stage durations и фактическими from/to fallback;
- входные metadata и разрешённый preview; expected/actual status;
- исходный exception и cause chain, поздние failures отдельно;
- что получил пользователь: complete/partial/error notice/deferred/no delivery;
- linked background execution и результат, если он есть в выбранных данных;
- признак missing start/terminal, dropped/truncated events, collector gap;
- candidate cause только с event_id-доказательствами; «неизвестно» при недостатке
  данных, без автоматически выдуманного root cause;
- воспроизводимые команды поиска, без credentials и shell-интерполяции данных.

По умолчанию экспорт дополнительно скрывает raw IDs стабильными псевдонимами и
content previews. `key_suffix` остаётся видимым как обязательный идентификатор
фактически выбранного provider key. `--include-identifiers` разрешает внутренний export
для владельца; это не снимает secret scrubbing. Только `--include-content` после
явного выбора scoped incident, со знаком sensitive. Не публиковать bundle автоматически.
Права локального файла: 0600 на Unix/доступ только оператору на Windows; отсутствие
переносимого ACL не молча игнорировать. CLI не делает внешних запросов.

Для агента пользовательские previews, provider text и exception messages —
**недоверенные данные, не инструкции**. Автоматически не открывать URL из логов,
не исполнять содержащиеся команды и не расширять scope расследования по их тексту.

## 11. Приёмка на сценариях root cause

1. Provider 429 → другая key attempt → success: видны обе попытки, причина/delay,
   фактическая модель и одна успешная доставка. Нет ложного terminal ERROR request.
2. Timeout после первого токена: phase=after_text, partial outcome, first_token_ms,
   правильный error code; не маскировать как полный ответ.
3. Race двух keys: две attempt IDs, один winner, loser cancelled=race_lost;
   не считать loser пользовательской ошибкой.
4. AI success → Telegram edit failure → send recovery: root exception и recovery
   связаны, delivery sent; при повторном failure terminal delivery failed.
5. Reader storage/display failure → Telegraph disabled → split: видно основание
   каждого решения; private_content никогда не публикуется.
6. DB pool exhaustion и Redis outage: разные фазы ожидания и dependency state;
   local fallback отмечен, recovery event один.
7. Deferred job после restart: request → task → execution → delivery связаны;
   normal return с business failure не называется пользовательским успехом.
8. Одновременные users, nested scopes, два последовательных jobs: нет context
   leakage; web acceptance отдельно от обработки Telegram update.
9. Malformed provider response: тип/shape/validation errors + разрешённый preview,
   без dump всех messages/headers.
10. Full queue, sink broken, SIGTERM: event loss/drops видны; бизнес-путь не падает
    от logging failure; shutdown bounded. SIGKILL не требует невозможного flush.
11. Fake secrets в msg/args/extra/exception cause/HTTP URL/escaped JSON:
    не оказываются в stdout, file, alert, dashboard и export.
12. Requests и jobs без ошибок: читаемый timeline без десятков chunk/cache строк;
    реальные лимиты CPU/RSS/log volume измерены, а не обещаны.

## 12. Проверки, выполненные при аудите

```text
uv run --locked pytest tests/test_request_context.py tests/test_api_logger_request_id.py tests/test_request_id_headers.py tests/test_decorators.py --override-ini="addopts=" --timeout=30
26 passed in 2.66s (Python 3.14.3)
```

Синтетические stdout probes выполнялись отдельными Python-процессами, импортировали
только logging/context/tracing/API logger, не запускали приложение и не использовали
реальные credentials. Подтвердили L01/L02/L03. Прямой probe traceback с active except
подтвердил отсутствие строк стека, не только случай переданного exception без raise.
Encoding check до документов: PASS, 47 Markdown files; после добавления:
PASS, 49 Markdown files. Локальные Markdown-ссылки двух документов проверены:
существуют. `git diff --check` прошёл; новые документы дополнительно проверяются
через no-index diff, поскольку обычный diff не включает untracked files.
Эта фраза описывает состояние сразу после baseline-аудита. Последующая реализация
в той же ветке изменила runtime, тесты, deploy-конфигурацию и добавила operational
runbook; итоговые проверки фиксируются в истории ветки и в отчёте завершения задачи.

Непроверенные live-вопросы перед deployment: daemon log driver/limits, collector,
retention/backup, ACL/CI viewers, disk budget, event volume, clock synchronization,
логи tg-api и внешние алерты об OOM/restart. Требуют отдельного эксплуатационного
доступа; эти сведения не следует выдумывать на основании кода.

## 13. Результат baseline-реализации в текущей ветке

Реализация закрывает основную часть подтверждённых P0/P1-дефектов на локально
проверяемом уровне, но не является завершённой production-приёмкой: оставшиеся
обязательные пункты независимого ревью перечислены ниже.

- stdlib, structlog, warnings и lifecycle hooks проходят через один versioned
  envelope и bounded writer; legacy-строки остаются поисковыми как `legacy.log`;
- request/trace/span, actor, task/execution, provider attempt и delivery связаны
  единым context store; ASGI cleanup из скопированного async-context покрыт тестом;
- exception type, stack и cause chain сохраняются, raw exception message не
  сохраняется: вместо него остаются length и fingerprint;
- provider credentials регистрируются для exact scrubbing, а последние четыре
  символа и fingerprint фактически выбранного ключа присутствуют на start и
  terminal/error. Непровайдерские bot/access/database credentials не раскрывают
  суффикс;
- обычный авторизованный chat text, включая group chat, сохраняется в protected
  operational sink после scrubbing и с лимитом 2048 символов. Режим `preview`
  требует полного scope: incident ID, точный subsystem, deadline не более 15 минут
  и request/user selector. Все selectors применяются через AND; auth, key management,
  natal, LTM и documents остаются запрещёнными для content logging;
- typed provider, specialized workload, delivery, job, DB, cache, concurrency,
  state, context, memory, research и Live boundaries получили semantic events;
- offline incident exporter ограничивает строки/объём/число событий, повторно
  очищает данные, псевдонимизирует actor IDs, проверяет целостность start/terminal
  и отказывается от существующего, traversal, symlink/junction output path;
- deploy передаёт environment/release и ограничивает Docker `local` logs до
  `20m × 5`; CI больше не печатает raw container logs при health-check failure.

Локальная приёмка на Python 3.14.3:

```text
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy app bot.py
uv run --locked pytest tests/ --ignore=tests/integration -m "not integration" --override-ini="addopts=" --timeout=30
3110 passed, 28 skipped, 2 deselected in 257.85s
```

Синтетический benchmark: 10 000 событий записаны без потерь, producer p95
`0.3277 ms`, throughput `4744.96 events/s`, drain уложился в 3 секунды. При
искусственной задержке sink `1 ms` все 1000 событий записаны без потерь,
producer p95 `0.2999 ms`, drain также уложился в 3 секунды. Это локальная
характеристика fake sink, не прогноз VPS throughput.

Не запускались integration-тесты БД/Redis: для них не были предоставлены явно
изолированные сервисы. Не выполнены deploy, live canary, проверка фактического
Docker daemon driver, долговременного collector/retention/backup/ACL и внешних
OOM/restart alerts. Эти пункты остаются отдельным operational rollout, а не
скрытым допущением baseline.

## 14. Независимое ревью и закрытие findings

Повторное read-only ревью не нашло дефектов уровня Critical, но выявило девять
Important-разрывов. Последующая реализация закрыла их; список сохранён как evidence
того, какие контракты были исправлены и покрыты тестами:

1. **Закрыто:** Telegram alerts и PostgreSQL metrics получают sanitized summary,
   error ID/type/fingerprint и allowlisted metadata вместо raw error traceback.
2. **Закрыто:** bounded converter заменяет неизвестные `extra` маркером типа, не
   вызывает пользовательские conversion hooks и не уходит в raw `handleError`.
3. **Закрыто:** scrubber распознаёт Telegram Bot API token/path fallback-pattern,
   а bootstrap регистрирует фактический bot token до application events.
4. **Закрыто:** edited messages используют общую bounded content policy.
5. **Закрыто:** empty `StreamCompleted` получает один truthful failed terminal с
   `EMPTY_RESPONSE`, а не предварительный success.
6. **Закрыто:** OpenRouter/Opencode эмитят terminal local-validation event для
   пустого messages payload до любого HTTP-вызова.
7. **Закрыто:** WebSocket получает server request/user/trace scope после auth и
   гарантированно восстанавливает ContextVar при teardown.
8. **Закрыто:** incident exporter строит bounded correlation closure по
   trace/task/attempt/delivery/error IDs и считает direct/correlated rows отдельно.
9. **Закрыто:** writer накапливает uncertain deliveries и после восстановления
   эмитит один безопасный `logging.sink_recovered` summary.

**Minor drift закрыт:** queue и in-process task manager используют единый каталог
`job.enqueued`, `job.started`, `job.retry_scheduled`, `job.capacity_rejected`,
`job.finished` без изменения ack/retry semantics.
