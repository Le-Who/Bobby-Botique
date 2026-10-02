# Модели, очереди и dashboard: исследование и предложение

Дата: 2026-09-29. Основа: checkout `25e54f8a` и фактическое рабочее дерево,
включая незавершённые изменения research budget. Это предложение, а не описание
уже внедрённого интерфейса. Production, аккаунты провайдеров и доступность моделей
не проверялись; идентификаторы ниже описывают код, а не рекомендуемый каталог API.

## Вывод

Уместно унифицировать описание и разрешение политик выбора моделей, их
проверку, отображение и историю изменений. Исполнение оставить специализированным:
streaming chat, agentic research, structured generation, изображения, embeddings
и Live имеют разные условия успеха, отмены и совместимости.

Администратору нужен единый раздел «Модели и маршруты», где каждый процесс
имеет понятную цепочку и показывает, откуда взялась настройка, когда она
применяется и какие ограничения обусловлены реальным исполнителем.
Каталог пользовательских кнопок должен остаться отдельным от внутренних ролей.

## Что уже существует

| Область | Фактическая реализация | Следствие для общего интерфейса |
| --- | --- | --- |
| Роли и каталог | [config.py](../app/config.py), [models_repo.py](../app/repos/models_repo.py): env baseline, внутренние роли, v2 admin overrides каталога, явный `none` | Показывать отдельно «доступно пользователю» и «используется процессом»; reset означает удаление override |
| Chat и ключи | [router.py](../app/providers/router.py), [agent_use_cases.py](../app/agent_use_cases.py): fallback порядка моделей, выбор/резервирование ключей, модельные и ключевые гонки | Порядок старта не равен гарантированному выбору первой модели; раскрывать вложенные попытки |
| Обычный поиск | [ai_search.py](../app/handlers/ai_search.py): отдельные chains для режимов поиска | Отдельные процессы, а не единственный переключатель «search model» |
| Agentic research | Тот же handler задаёт primary и сортирует остаток каталога по tier; [agentic.py](../app/core/agentic.py) вызывает Gemini напрямую; [research_budget.py](../app/core/research_budget.py) задаёт общий бюджет | Настройка порядка должна учитывать реальный общий бюджет и tool compatibility; каталог не должен неявно переписывать утверждённую цепочку |
| Crocodile | [daily_ai.py](../app/games/daily_ai.py): words/category/hints/judge/image_prompt, process override → общий legacy override → auto; явная Gemini модель вызывается напрямую | Сохранить наследование и режим фиксированной модели; auto раскрывает фактический специальный маршрут |
| Crocodile auto | [judge.py](../app/games/judge.py), [word_bank.py](../app/games/word_bank.py): отдельные модели, races и бюджеты | Не заменять настройку одной ролью «игры» |
| Trivia | [daily_trivia.py](../app/games/daily_trivia.py): primary → retry model → primary fallback → economy, dedupe; executor проверяет parsing | Ошибка формата и повтор авторинга — отдельные причины повторной попытки |
| Summary | [token_budget.py](../app/context/token_budget.py), [summarizer.py](../app/context/summarizer.py): константа модели и локальные уровни сжатия | Показать и AI, и локальное завершение; локальная обработка не является моделью |
| Память | [memory_config.py](../app/repos/memory_config.py): embedding, expansion, consolidation, extraction, taxonomy | Разделить процессы; embedding model и размерность нельзя менять как chat preference |
| Inline и tarot | [inline.py](../app/handlers/inline.py), [tarot_daily.py](../app/tarot_daily.py), [tarot_chat.py](../app/handlers/tarot_chat.py): несколько отдельных констант и путей | Нужны отдельные процессы inline answer, grounding, tarot inline/daily/chat |
| Natal | [natal/llm.py](../app/natal/llm.py): primary, repair структурированного ответа и локальный fallback | Repair — стадия процесса; локальный отчёт — допустимый исход, не запасная LLM |
| ASR и media intent | [multimodal_processor.py](../app/utils/multimodal_processor.py): Whisper, Gemini ASR chain, отдельная классификация intent | Нельзя передать audio blob обычной text-only модели; этапы настраиваются отдельно |
| Image | [imagen_provider.py](../app/providers/imagen_provider.py): нормализует выбор в поддерживаемую image модель; Daily имеет отдельный provider catalog/default | Поле произвольного ID бесполезно, если исполнитель затем заменяет его; это нужно исправлять на границе исполнителя при внедрении |
| TTS | [voice_engine.py](../app/voice_engine.py), `_pregenerate_audio()`: ElevenLabs при наличии ключей → Gemini TTS 3.1 → Gemini TTS 2.5; IDs Gemini переданы строками | Реальный fallback находится у вызывающего workflow; неиспользуемая константа в `tts.py` не означает отсутствие fallback. Сохранить FIFO, chunks, разные голоса и consent lease |
| Live | [web_miniapp.py](../app/web_miniapp.py): разные Vertex/AI Studio модели и получение session credentials | Новая настройка действует на новые сессии; переключение посреди потока требует отдельной реализации |

Карта выше — подтверждённые точки входа, не заявление об исчерпывающей миграции
всех provider calls. Перед включением записи настроек нужен реестр call sites:
каждый вызов получает process ID либо документированное исключение. Это включает
horoscope, документы/Q&A, URL selection, translation, vision, image edit,
прочие audio/image providers и фоновые jobs; нельзя считать их покрытыми лишь
потому, что они вызывают общий router.

### Дополнительная проверка вызывающих процессов

Повторный проход по callers уточняет карту. Предлагаемые идентификаторы ниже —
названия для будущего реестра, не существующие API. Общие SDK adapters не получают
самостоятельную пользовательскую карточку вместо вызывающих их процессов.

| Предлагаемый процесс | Проверенный caller и текущий выбор | Особенность настройки |
| --- | --- | --- |
| `documents.answer` | [ai_document.py](../app/handlers/ai_document.py): `models=(settings.DEFAULT_MODEL,)`, typed delivery | Проверять effective route, а не только поле роли QNA: этот caller читает DEFAULT_MODEL |
| `vision.ocr` / `vision.answer` | [ai_photo.py](../app/handlers/ai_photo.py): OCR выбирает доступную primary/economy; обычное фото использует chat model с заменой image-generation-only выбора | Сохранить разделение распознавания текста и ответа по изображению |
| `vision.intent` | [vision_intent.py](../app/utils/vision_intent.py): INLINE_MODEL, 5 секунд, при неудаче `describe` | Классификатор не равен модели анализа картинки; regex fast path сохраняется |
| `vision.search_query` | [ai_photo.py](../app/handlers/ai_photo.py), [ai_search.py](../app/handlers/ai_search.py): подготовка поискового запроса через выбранную vision/research модель | Отделить генерацию запроса от последующего поиска и финального ответа |
| `voice.intent` | [voice_intent.py](../app/voice_intent.py): OPENCODE_INLINE_MODEL либо OPENCODE_QNA_MODEL, resolver + direct provider, 12 секунд | При недоступности не включать TTS автоматически |
| `horoscope.generate` | [intent_router.py](../app/intent_router.py), `_handle_horoscope()`: `gemini-3.5-flash`, typed router, INTERACTIVE, deferred запрещён | [Рассылка](../app/handlers/scheduled_horoscopes.py) вызывает тот же путь. Нельзя вывести из названия cron-job, что здесь уже используется background routing |
| `brief.summarize` | [scheduled_briefs.py](../app/handlers/scheduled_briefs.py): прямой Gemini SDK с `gemini-3.1-flash-lite`, JSON summary | Сохранить lease для приватной LTM и проверку эпохи; public/private branches — один процесс с разными privacy requirements |
| `trivia.explain` | [commands.py](../app/handlers/commands.py): deep-link объяснение факта через DEFAULT_MODEL и legacy router | Это отдельный caller от генерации daily trivia; при exception показывается сохранённый факт |
| `roles.generate` | [cb_roles.py](../app/handlers/cb_roles.py), [msg_roles.py](../app/handlers/msg_roles.py): resolver и `_get_ai_response`, включая модель chat state | Не пропустить создание/перегенерацию пользовательских ролей при миграции chat |
| `image.translate` / `image.enhance` | [cmd_image.py](../app/handlers/cmd_image.py): Gemini SDK, `_TRANSLATE_MODEL` | Подготовка prompt имеет собственную модель и бюджет, отдельно от image generation |
| `image.generate` | [cmd_image.py](../app/handlers/cmd_image.py): dispatch в Gemini image, FreeTheAI image либо Pollinations по выбранной модели | Это ветвление выбора provider, не автоматическая fallback-цепочка трёх провайдеров |
| `image.edit` | [cb_image.py](../app/handlers/cb_image.py): отдельные обращения с input image и draw state | Требование image-input capability; сохранить aspect ratio и state |
| `inline.image` | [inline.py](../app/handlers/inline.py): собственные вызовы image providers | Общий image adapter не гарантирует общего deadline и publication поведения |
| `audio.music` | [ai_chat.py](../app/handlers/ai_chat.py): FreeTheAI audio `provider.generate()` с выбранной моделью | Генерация музыки не является TTS или Live; отдельный тип результата |

Исключения тоже должны быть видны в реестре: deterministic game logic и внешние
не-LLM источники не требуют выбора AI-модели. Например, preparation
[Daily 2048](../app/games/daily_2048.py) вызывает repository `ensure_puzzle`, а
weather/currency paths в `intent_router.py` работают с HTTP data sources.
Их API fallback относится к политике источников данных, не к списку LLM.
Диагностические admin/ASR команды следует обозначить diagnostic-only, отдельно
от пользовательских процессов. Объявленное `URL_SELECTION_MODEL` само по себе
не доказывает отдельный активный workflow: нужен подтверждённый caller, а не
автоматическое создание карточки по каждому полю `Settings`.

## Очереди: разные механизмы и гарантии

| Механизм | Текущий контракт | Что показывать |
| --- | --- | --- |
| [TaskQueue](../app/queue.py) | Redis priority lists, processing list, recovery при старте; локальная PriorityQueue при fallback | Backlog по backend, local running/completed/failed отдельно, durability и ограничения recovery |
| [TaskManager](../app/utils/background_tasks.py) | Отслеживаемые локальные coroutine, factory retries, drain/cancel | Active tasks, capacity, retry count; это не durable backlog |
| [Concurrency](../app/adapters/concurrency.py) | Локальный semaphore и Redis ZSET для heavy/ultra-heavy admission | Active/waiting, область действия лимита, degraded local mode |
| Provider attempts | Model/key races и последовательные попытки внутри одного запроса | Число запросов и provider calls отдельно, hedge delay, winner, причина перехода |
| [Game budgets](../app/games/ai_budget.py), [voice](../app/voice_engine.py), research | RPM/cooldown, отдельные concurrency и budgets | Назначение ограничения, расход и момент применения настройки |

Обнаружены следующие риски чтением кода. Memory fallback дополнительно
воспроизведён локально с mock Redis (см. проверку ниже). Остальные пункты
не воспроизводились; production не проверялся. Исправления требуют отдельных
регрессионных тестов.

- `TaskQueue.add_task()` после ошибки Redis помещает задачу в memory queue,
  но `_dequeue_task()` при существующем Redis client выходит после пустого
  Redis/ошибки, не читая memory queue. Возможна остановка локального backlog.
- Startup recovery переносит весь общий processing list обратно в очередь,
  не проверяя lease живого worker. Это не гарантия безопасного нескольких
  экземпляров; возможна повторная обработка при перекрывающемся старте.
- `cancel_task()` меняет локальный status, но не отменяет running coroutine.
  Worker затем может присвоить COMPLETED. Не обещать остановку уже начатой работы.
- `get_queue_stats()` смешивает Redis backlog и локальные статусы, а размер
  выбирается через `redis_queue_size or memory_size`, не суммируется.
- Возвращённый handler результат `{"status": "failed"}` завершает Task как
  COMPLETED; retries запускаются при exception. Observability различает business
  failure, но UI статуса задачи может показывать другое.
- Semaphore ловит `UserLimitExceededError` собственным широким `except Exception`
  и допускает local fallback после истечения ожидания глобальной ёмкости.
  Фиксированный срок ZSET entry без heartbeat также не равен renewable lease.
- Lazy semaphore создаёт ресурс один раз: изменение поля настройки не доказывает
  изменение уже действующего лимита.

Эти пункты нужно устранить или явно отразить в контракте перед предоставлением
редактируемых concurrency/retry/cancel controls. Косметическое обновление экрана
само по себе не исправляет семантику исполнения.

## Почему сейчас настройки трудно объяснить

1. `_gemini_fallback_priority()` в `agent_use_cases.py` и
   `_ordered_gemini_fallback_models()` в `router.py` независимо строят похожий
   порядок из runtime constants, ролей и каталога. Изменение одного списка
   не гарантирует изменение всех путей.
2. Resolver ключей способен вернуть другую модель ещё до router retry.
   Редактор только внешнего массива моделей не обеспечивает строгий порядок.
3. Research сортирует каталог по эвристическому tier, а не просто читает его
   порядок. Это отдельная неявная политика.
4. `global_settings` кешируется на 30 секунд и при ошибке чтения возвращает
   default. Для критичной маршрутной политики нельзя выдавать эту ситуацию
   за успешно прочитанный env baseline.
5. `ConfigManager.update_setting()` — изменение в памяти для debug/testing,
   не durable административная запись. Reload восстанавливает env и вызывает
   watchers; новый экран обязан иметь согласованную persistence/reload модель.
6. Текущий экран показывает агрегат одной task queue. Это не общая картина
   ожидающих Telegram updates, лимитов провайдеров, research и игровых jobs.
7. `GenerationRequest.models` не является закрытым планом: serial streaming
   и interactive hedge могут добавлять fallback. В Trivia
   `execute_gemini_model_plan()` проходит модели на выбранном ключе, затем
   меняет ключ. Общий редактор обязан отражать эту вложенность.

## Варианты

| Подход | Польза | Ограничение | Оценка |
| --- | --- | --- | --- |
| Форма поверх существующих env/DB полей | Быстрый доступ к нынешним настройкам | Не контролирует скрытые fallback, наследование и реальную очередность | Временный read-only обзор |
| Общая политика процессов + специализированные исполнители | Один источник правил, объяснимый порядок и свобода выбора в поддерживаемых границах | Потребуется последовательно подключить все call sites и проверить поведение | Рекомендуется |
| Одна универсальная очередь и executor для всех AI | Внешне одинаковая схема вызовов | Смешивает session, streaming, structured output, durable jobs и отмену; большой риск регрессий | Не рекомендуется |

## Предлагаемое поведение

### Выбор и последовательность

У процесса есть baseline и необязательный административный override. Пользовательский
выбор модели остаётся входом там, где он уже существует; карточка процесса явно
показывает, как он сочетается с административной политикой. Нельзя молча заменить
пользовательскую primary, добавив ещё один глобальный приоритет.

Администратор выбирает фиксированную модель, последовательные попытки или
поддерживаемый исполнителем отложенный параллельный старт. Для каждой цепочки
видны provider, model ID, условие перехода и общий лимит работы. Добавление
новой совместимой модели не должно требовать изменения закрытого enum.

Режим «наследовать», пустой список fallback и «отключить процесс» — разные
значения. Отключение предоставляется только процессам, где определён корректный
продуктовый результат. Начальные настройки воспроизводят существующее поведение.
При новой явной цепочке не добавляются скрытые модели из пользовательского каталога.

Ключи, cooldown и quota остаются ответственностью key management. Редактор
маршрута задаёт верхние бюджеты и стратегию, но не обходит suspended keys или
резервирование квоты. Общий budget ограничивает вложенные retries, чтобы
«три модели × три ключа × три повтора» не превращались в неожиданные 27 вызовов.

### Совместимость без закрытого каталога

Проверка состоит из синтаксиса ID, требований процесса и сведений провайдера.
Результаты различаются: совместима, несовместима, неизвестно, проверка недоступна.
Наличие в списке не доказывает доступность аккаунту. Неизвестная новая модель
не блокируется только из-за отсутствия в статическом списке: допускается явное
подтверждение неподтверждённой capability, если adapter поддерживает такой запрос.
Заведомо несовместимые типы payload блокируются сервером.

Embedding model имеет отдельную процедуру re-embedding/index migration с оценкой
совместимости, даже при одинаковой размерности. Live применяется на новых сессиях.
Настройки startup-owned workers требуют перезапуска либо отдельно реализованного
drain/resize; интерфейс не должен обещать hot reload там, где его нет.

### Сохранение и применение

Сначала черновик, затем серверная проверка и dry-run разрешения маршрута без
provider calls. Перед применением показываются diff и затронутые процессы.
Запись версии атомарная, с проверкой ожидаемой revision: две открытые вкладки
не перезаписывают изменения друг друга. Ошибка persistence не становится успехом UI.

Новый запрос получает immutable snapshot политики; выполняющаяся задача продолжает
с прежней версией. Для реплик явно показываются сохранённая и применённая revision.
При недоступности хранилища используется последняя подтверждённая политика с
degraded status; cold start без такой версии имеет явно определённый baseline.
История позволяет вернуть предыдущую версию и отдельно сбросить override к baseline.

Существующие игровые настройки и `/models` должны писать через согласованную
границу после подключения соответствующего процесса. Не поддерживать два независимых
редактора одного effective value. Переходное чтение legacy ключей должно быть
помечено источником и покрыто тестами; удаление старых ключей — отдельная миграция.

Новые write endpoints используют существующую admin auth boundary, проверку
подлинности browser mutation, ограничение размера payload и серверную валидацию.
Аудит фиксирует actor, revision, изменённые поля и результат без credentials
и пользовательского содержимого. Это не редактор секретов.

## Dashboard

Сохранить Quart/Jinja и существующие JSON endpoints; переход на SPA-фреймворк
сам по себе не решает задачу. Вынести поведение страницы в небольшие JS modules,
использовать общие CSS tokens и компоненты форм. Основная навигация:

- Обзор: состояние, задержка, ошибки, последний успешный snapshot.
- Модели и маршруты: поиск/фильтры процессов, source badges, редактор цепочки.
- Очереди и нагрузка: отдельные admission, durable queue, local jobs и leases.
- Провайдеры: доступность, квоты, key health, circuit breakers.
- Ежедневные процессы: существующие admin controls, подготовка и рассылки.
- Ошибки и изменения: текущие errors и история административных политик.

В списке процессов показывать назначение, primary, число fallback, стратегию,
источник, applied revision и признак необходимости перезапуска. В деталях —
цепочку карточек с кнопками вверх/вниз; drag-and-drop может быть дополнением,
но клавиатура и touch должны работать без него. Advanced-поля раскрываются
по запросу; неподдерживаемая опция объясняется, а не скрывается молча.

В редакторе полезны сценарии «primary timeout», «нет ключа», «ошибка JSON»:
предпросмотр показывает, какая попытка последует, почему и какой бюджет останется.
Это симуляция политики, не проверка провайдера и не обещание задержки.

Текущая страница уже имеет tabs, sparklines, poll `/api/dashboard` каждые 15 секунд
и SSE `/api/events`. Новая версия сохраняет существующие показатели CPU, memory,
disk, services, requests, response time, errors, cache, summary, Gemini/Tavily keys,
key health, breakers, DB pool и task queue. Актуальность должна определяться
успешным обновлением данных; network error нельзя маскировать постоянным LIVE.
Разделять unavailable, stale, empty и числовой zero. Обновление данных не должно
сбрасывать форму, focus, раскрытые детали или несохранённую цепочку.

## Порядок внедрения и доказательства готовности

1. Реестр процессов и read-only effective policies. Для каждого call site —
   источник, executor, capabilities, fallback и способ применения. Сравнить
   разрешённые маршруты с текущим поведением, включая старые admin overrides.
2. Общее разрешение политик и snapshot без смены default поведения. Проверить
   пустые списки, приоритеты, aliases, произвольные совместимые ID, races и
   отсутствие неявной подмены модели при строгой цепочке.
3. Versioned persistence, validate/preview/apply/reset/history. Проверить concurrent
   edits, недоступную БД, invalid records, restart и несколько экземпляров приложения.
4. Подключить chat/search/inline, затем structured/game/memory, затем media/Live.
   На каждом этапе UI явно показывает поддержанные и ещё неподключённые процессы.
   Подключение одной роли не считается покрытием всей подсистемы.
5. Обновить dashboard и связать редактор с тем же backend resolver. Проверить
   browser flows desktop/mobile, keyboard, ошибки save, stale snapshots,
   отсутствие потери существующих admin actions и формы при polling/SSE.
6. Проверить полный перечень процессов и согласованность UI → persistence →
   resolved snapshot → фактический executor. Только после этого считать общий
   интерфейс готовым. Live provider/VPS checks отдельно от локальных тестов.

Обязательные регрессии: первый видимый текст закрепляет stream winner; ровно
один terminal event; отменённые races завершаются и awaited; quotas не обходятся;
research budget общий; явная Crocodile модель сохраняется; memory consent и
provenance не меняются; Live session не переподключается скрыто; queue retry
не создаёт повторную пользовательскую доставку. Сохраняются
[ADR 0001](adr/0001-single-owner-ai-response-delivery.md) и
[ADR 0002](adr/0002-provenance-safe-memory-graph-writes.md).

## Состояние работы

Исследованы перечисленные исходники и текущая структура dashboard. Runtime,
настройки deployment и база не изменялись. Этот документ задаёт предложенный
результат и критерии его проверки; он не подтверждает выполнение этапов внедрения.

Это состояние первоначального исследования. После разрешения пользователя
начата реализация по [плану](superpowers/plans/2026-09-29-routing-dashboard-implementation.md).
В рабочем дереве исправлены memory fallback dequeue, сумма Redis/local backlog,
отказ по global admission timeout и освобождение слота при отмене Redis acquire.
Остальные риски выше остаются открытыми; полный editor/dashboard ещё не внедрён.
Пользователь подтвердил включение предустановленных промптов и разрешил
реорганизацию ресурсов для обновления без лишних перезапусков.

### Дополнительные свидетельства 2026-09-29

- `uv run --locked pytest tests/test_task_queue.py tests/test_redis_queue.py
  tests/test_semaphore_invariants.py --override-ini="addopts=" --timeout=30`:
  **31 passed**. Redis в этих проверках подменён; semaphore invariants проверяют
  локальный путь. Результат не доказывает distributed guarantees.
- Отдельный диагностический вызов, без изменения runtime: после установки
  безопасных test settings из `tests/conftest.py`, `AsyncMock.lpush` бросает
  `ConnectionError`, а `rpoplpush` возвращает `None`. `add_task()` сохраняет
  одну local task. `_dequeue_task()` при том же существующем Redis client
  возвращает `(None, None)` и оставляет local queue размером 1. Контрольный
  вызов с `_get_redis() == None` извлекает ту же задачу, размер становится 0.
  Это воспроизводит конкретный memory fallback дефект; реальный Redis не нужен.
- Текущий тест `test_cancel_task` в
  [test_redis_queue.py](../tests/test_redis_queue.py) проверяет статус после
  отмены, а не остановку running handler.
  Поэтому успешный cancellation test не является доказательством такой остановки.
