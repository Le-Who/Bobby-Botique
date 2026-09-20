# Roadmap развития GemAI Bot v2

Дата анализа: **2026-09-20**, checkout **`f44541a7`**. Основание: код,
конфигурация, SQL, тесты и workflows; [отчёт аудита](docs/documentation-audit-2026-09-20.md)
описывает границы проверки. Это предложения, а не утверждённый график,
обещание релиза или разрешение выполнять миграции, платные запросы и deployment.
Текущие возможности описаны в [README](README.md) и [архитектуре](docs/ARCHITECTURE.md).

## Вывод и стратегия

Проект уже включает несколько AI-провайдеров, восстановление ответов, research,
приватную графовую память, голос, изображения, игры, Mini App, структурированные
логи и автоматизированный deployment. Следующий полезный шаг — сделать качество,
стоимость и восстановление этих сценариев измеримыми, а настройки предсказуемыми.

| Стратегия | Выигрыш | Ограничение | Когда выбирать |
| --- | --- | --- | --- |
| Стабилизация существующего продукта — рекомендуется первой | Меньше неясных отказов, измеримое качество и воспроизводимая эксплуатация | Меньше новых функций в первых итерациях | Пока нет свежей базовой линии качества/нагрузки и проверенного restore |
| Рост возможностей личного ассистента | Более полезные документы, память, голос и daily flows | Новые расходы и сложность | После базовых оценок и подтверждённых потребностей пользователей |
| Платформа с несколькими репликами и изоляцией арендаторов | Масштабирование и более сильные границы данных | Изменение владения состоянием, jobs, DB-ролей и deployment | При подтверждённой нагрузке или требовании нескольких арендаторов |

Рекомендуемый порядок: стабилизация → качество и UX → точечный продуктовый рост.
Платформенная перестройка — отдельное решение по измерениям.

## Приоритеты и объём

- **P0**: фундамент эксплуатации и достоверного измерения. Это приоритет roadmap,
  не утверждение о найденной аварии или уязвимости.
- **P1**: улучшение основного пользовательского опыта после P0.
- **P2**: расширение при подтверждённой пользе; можно отложить.
- **S**: 1–3 инженерных дня; **M**: 4–8; **L**: 9–15; **XL**: больше 15,
  нужна декомпозиция. Предварительные оценки одного исполнителя включают
  локальные проверки и документацию, но не ожидание доступа и эксплуатационные
  наблюдения. Они не являются календарными обязательствами.

Метрики ниже — будущие критерии оценки. Текущие значения этим аудитом не
измерены; численные SLO нужно выбрать после получения baseline.

## 1. Надёжность и эксплуатация

**Есть:** webhook dedup/backpressure, provider fallback, typed delivery,
управляемые tasks, Redis queue/deferred paths, exact-SHA deployment и health gates.
`/health` отражает DB connection flag и показывает Redis, но не проверяет providers.

**REL-1 · P0 · M — Матрица отказов и восстановление запроса.**

1. Зафиксировать ожидаемый результат для timeout/429, обрыва stream, Telegram
   edit/send failure, недоступного Reader, Redis loss и остановки процесса.
2. Расширить существующие тесты fake transports и управляемым временем;
   отдельно отметить неопределённость после внешней отправки и crash.
3. Разделить в runbook «генерация завершена», «ответ доставлен», «доставка
   неизвестна» и «требуется повтор». Не обещать exactly-once для внешней сети.

**Приёмка:** каждый сценарий имеет ожидаемый typed outcome, проверку финальной
клавиатуры, cleanup tasks и отсутствия непреднамеренной повторной отправки в
моделируемом контролируемом пути. Метрики: complete/partial/failed/deferred,
дубли и время восстановления.
**Опора:** [delivery](app/response_delivery/), [queue](app/queue.py),
[deferred](app/deferred_response.py), [recovery tests](tests/test_degradation_recovery.py),
[webhook lifecycle](tests/e2e/test_webhook_lifecycle.py).

**REL-2 · P0 · M — Проверяемое восстановление после потери хоста.**

Инвентаризировать PostgreSQL, необходимые Redis состояния, конфигурацию,
стабильный `ADMIN_SECRET`, внешние секреты и медиаданные. Написать backup/restore
runbook и выполнить восстановление в отдельном окружении. Проверить схему,
расшифровку тестового ключа, контрольные записи и запуск нужного SHA. Указать,
что теряется без Redis/медиа и не восстанавливается из локальных Loki/Docker logs.
**Приёмка:** измерены RPO/RTO, проверен restore, определены владелец, частота и
защищённое хранение копий. Production dumps не попадают в Git.
**Зависимость:** изолированная инфраструктура и согласованное окно проверки.

## 2. Наблюдаемость и диагностика

**Есть:** bounded NDJSON, correlation IDs, redaction, incident export, metrics и
приватный Grafana/Loki/Alloy. CI проверяет конфигурацию и readiness/auth, но не
утверждает, что новое событие бота действительно найдено поиском.

**OBS-1 · P0 · M — Сквозной synthetic smoke и baseline.**

В изолированном CI stack запустить контейнер с collector label и уникальным
безопасным `event_id`; дождаться его в Loki за ограниченное время и проверить
схему. Дополнить сценариями unlabelled-контейнера, malformed JSON и дубликатов.
Снять CPU/RAM, disk growth, скорость событий и p95 поиска на 100/1500/10000
синтетических строках существующим инструментом.
**Приёмка:** CI падает при потере события; отчёт содержит SHA, окружение,
лимиты и измеренные значения. Readiness и ingestion — разные проверки.
**Опора:** [smoke CLI](scripts/smoke_log_viewer.py), [stack](ops/observability/),
[collector tests](tests/observability/test_collector_contract.py),
[baseline](docs/operations/log-viewer-baseline.md).

**OBS-2 · P1 · M — SLO и полезные оповещения.**

Связать успешную доставку, TTFT/полную задержку, очередь, потерю логов и provider
exhaustion с окнами наблюдения. Расширять существующий
[admin alerts](app/admin_alerts.py), определить suppression и recovery notices.
Для каждого сигнала дать конкретное действие оператора.
**Приёмка:** искусственное нарушение вызывает понятный alert и recovery;
обычная смена provider не создаёт шторм, payload не содержит сообщения/секреты.
**Зависимость:** OBS-1 и REL-1; пороги выбираются по baseline.

## 3. Качество AI, маршрутизация и стоимость

**Есть:** Gemini/Opencode/OpenRouter/FreeTheAI, model roles и catalog overrides,
typed events, retries и отдельные media/Live paths. Новый provider сам по себе
не гарантирует улучшение качества.

**AI-1 · P0 · L — Воспроизводимый набор оценок.**

Собрать синтетические или разрешённые обезличенные RU/EN примеры для chat,
`?`, `??`, document Q&A, роли и отказов. Задать рубрику: соответствие задаче,
поддержка источниками, полнота и признание нехватки данных. Разделить deterministic
contract tests, ручную оценку и необязательный live прогон. LLM judge калибровать
по ручной выборке, не использовать как единственный критерий.
**Приёмка:** baseline/candidate сравниваются на одном наборе; отчёт фиксирует
модель/дату/параметры, качество, TTFT, полную задержку, usage и фактическую либо
явно расчётную стоимость. Отсутствие usage не означает нулевую стоимость.
**Опора:** [router](app/providers/router.py), [typed tests](tests/test_provider_stream_types.py),
[research tests](tests/test_agentic_search.py). Существующие dependency canaries
проверяют совместимость, а не заменяют продуктовую оценку.

**AI-2 · P1 · L — Политика выбора по задаче и бюджету.**

По результатам AI-1 определить capabilities chat/search/research/vision, бюджет
retry/fallback и причины смены модели. Сохранить явный выбор пользователя/admin
и пустые каталоги `none`. Начать с понятной конфигурационной таблицы и учёта
попыток, без самообучающегося роутера.
**Приёмка:** quality gate не ухудшается; retries учитываются в бюджете,
недопустимая capability отклоняется до запроса. Direct Crocodile SDK,
embeddings и Live/TTS сохраняют собственные контракты.
**Зависимость:** AI-1, OBS-1.

**AI-3 · P0 · M — Общий deadline и бюджет research.**

Сейчас `app/core/agentic.py` проверяет время и накопленные токены между
итерациями; текущий вызов и финальный синтез могут выйти за порог. Handler
создаёт новый agent run при fallback, со свежими счётчиками. Спроектировать
бюджет всего пользовательского запроса, резерв на синтез и cancellation
in-flight calls; учитывать retries/fallback вместе. При отсутствии usage
явно показывать неопределённость расхода.
**Приёмка:** fake clock/transport подтверждают общий deadline, корректный
cleanup и partial outcome; все попытки отражены в учёте. Token reservation
не называется гарантированной provider cost без подтверждённого usage.
**Опора:** [agent loop](app/core/agentic.py), [handler](app/handlers/ai_search.py),
[tests](tests/test_agentic_improvements.py). **Зависимость:** REL-1.

## 4. Research, документы и источники

**Есть:** bounded agentic search, URL selection/extraction, context budgets,
парсинг и chunking документов. Тесты оркестрации не измеряют качество цитат.

**RAG-1 · P1 · L — Ответы с проверяемыми основаниями.**

Подготовить корпус с известными ответами, отсутствующими фактами, противоречиями
и длинными таблицами. Прослеживать ответ до документа/страницы/фрагмента;
измерять retrieval recall, точность цитат и корректный отказ. Проверить timeout,
нечитаемые файлы, недоступные страницы и инструкции, внедрённые во внешние данные.
**Приёмка:** подтверждённые и неподтверждённые утверждения различимы; цитата не
фабрикуется при отсутствии основания. Budget exhaustion явно ограничивает полноту.
**Опора:** [research](app/core/agentic.py), [documents](app/documents/),
[context](app/context/), [tests](tests/test_agentic_improvements.py).
**Зависимость:** AI-1.

**RAG-2 · P2 · M/L — Актуальность пользовательского корпуса.**

Проверить текущие операции `/documents`, затем определить версии документа,
повторную индексацию, статус обработки и удаление связанных chunks. OCR/новые
форматы добавлять по реальным проблемным файлам с корпусом и оценкой стоимости.
**Приёмка:** новая версия не смешивается незаметно со старой, удалённый документ
не участвует в retrieval, повтор обработки имеет понятный результат.
**Зависимость:** RAG-1.

## 5. Приватная память и персонализация

**Есть:** consent epochs/leases, private-chat scope, hybrid retrieval, graph
provenance, atomic writer, `/memory`, `/clearmemory`, export/deletion.

**MEM-1 · P1 · L — Полезность и свежесть памяти.**

Создать сценарии предпочтения, изменения факта, противоречия, expiry, удаления
источника и отключения LTM во время provider call. Оценивать retrieval и итоговый
ответ отдельно, сравнивать с memory-disabled baseline. Измерять ложный recall
и ненужное включение личных фактов.
**Приёмка:** удалённые/отозванные источники не возвращаются, обработка не
оживляет старую epoch; опубликованы результаты на синтетическом корпусе.
**Опора:** [consent](app/repos/memory_consent.py), [writer](app/repos/memory_graph_writer.py),
[ADR 0002](docs/adr/0002-provenance-safe-memory-graph-writes.md),
[consent tests](tests/test_memory_consent.py), [provenance tests](tests/test_memory_extraction_provenance.py).

**MEM-2 · P1 · M/L — Управляемость памяти.**

На базе `/memory` и Mini App уточнить показ «что сохранено, откуда и насколько
актуально», точечное исправление/удаление и состояние согласия. Исправление
должно согласованно менять provenance, граф и retrieval, а не только UI.
**Приёмка:** путь просмотр → исправление → повторный вопрос → удаление проверен
через интерфейс и хранилище. **Зависимость:** MEM-1. Групповая память — отдельная тема.

## 6. Приватность, безопасность и доступ к данным

**Есть:** отдельные admin/Mini App/webhook/WebSocket guards, Fernet keys,
Telegraph opt-in, redaction. Single migration/runtime DSN допускает обход RLS
привилегированной ролью; это известная граница, не доказательство утечки.

**SEC-1 · P0 · M — Матрица доступа, хранения и удаления.**

Описать доступ пользователей/операторов/сервисов к LTM, Reader, reports,
экспорту, метрикам и логам. Уточнить информирование о bounded message text в
защищённых логах (`full` по умолчанию), mode `metadata`, retention и публичных
ссылках. Проверить negative auth cases и erasure races на фиктивных данных.
**Приёмка:** матрица связана с allow/deny tests; ограничения `/metrics` заданы
явно; нет обещания универсального удаления внешних копий.
**Опора:** [policy](SECURITY.md), [web](app/web.py), [Mini App](app/web_miniapp.py),
[logging](docs/logging.md), [security tests](tests/test_web_security.py).

**SEC-2 · P1 при требовании hard isolation, иначе P2 · L — Роли БД.**

Спроектировать migrator и non-owner runtime без `BYPASSRLS`; проверить права
DML/sequences/functions, tenant context и startup validation. Решать вопрос
`FORCE ROW LEVEL SECURITY` после проверки всей цепочки provisioning.
**Приёмка:** реальный PostgreSQL под runtime-ролью запрещает cross-tenant
read/write, обычные flows проходят, миграции выполняет migrator; есть переход
и восстановление без потери данных. **Зависимость:** SEC-1, REL-2, отдельный ADR
и изолированные integration tests.

## 7. UX, Mini App и доступность

**Есть:** RU/EN command catalog, помощь по категориям, settings, Reader,
игры и формы; accessible names/labels уже улучшены и покрыты
[template tests](tests/test_template_accessibility.py).

**UX-1 · P1 · M — Основные пользовательские маршруты.**

Проверить первый запуск → модель/роль → сообщение → long read → сохранение;
отдельно consent, document Q&A и voice. Составить матрицу Telegram mobile/desktop,
размеров экрана, keyboard/focus и screen-reader names. Проверять loading/empty/
error/retry, закрытие Mini App, смену языка и объяснение partial response.
**Приёмка:** critical flows воспроизводимы с fixtures в browser tests и имеют
датированную ручную проверку в Telegram; динамическое имя микрофона следует
состоянию. HTML-тест не выдаётся за реальную mobile-проверку.
**Опора:** [templates](app/templates/), [catalog](app/bot_commands.py), [i18n](app/i18n.py).

**UX-2 · P2 · M — Упростить навигацию по данным использования.**

Исследовать затруднения с выбором модели, `?`/`??`, изображениями, памятью и
daily flows. Предложить быстрые действия/пресеты на существующем каталоге,
сохранив экспертные настройки.
**Приёмка:** выбранные задачи выполняются с меньшим числом ошибок/шагов;
измерение не собирает текст сообщений. **Зависимость:** UX-1 и согласованный
способ получения обратной связи.

## 8. Голос, Live и изображения

**Есть:** STT, intent routing, queued TTS, ElevenLabs → Gemini fallback,
Gemini/Vertex Live, canvas и несколько image providers.

**MEDIA-1 · P1 · L — Сбои и совместимость медиа.**

Проверить пустое/повреждённое аудио, permission denial, отмену, reconnect,
закрытие Mini App, exhausted keys и Telegram upload failure. Для image flow —
выбранную модель/размер, unavailable selection и повтор доставки готовой картинки.
Измерить время до первого звука/картинки и полную задержку; ограничить расходы
и параллелизм live-проверок.
**Приёмка:** отмена закрывает соединения/tasks, fallback не смешивает
незавершённые голосовые результаты; generation failure отличима от delivery failure.
**Опора:** [voice engine](app/voice_engine.py), [TTS](app/providers/elevenlabs_tts.py),
[Mini App](app/web_miniapp.py), [image handler](app/handlers/cmd_image.py),
[tests](tests/test_elevenlabs_tts.py). **Зависимость:** REL-1, AI-1, UX-1.

**MEDIA-2 · P2 · M — Профили качества и бюджета.**

После MEDIA-1/AI-1 предложить режимы «быстро/качественно» с проверенной
конфигурацией, доступностью модели и понятной обработкой её недоступности.
**Приёмка:** профиль сохраняет явный выбор и нужные flow confirmations,
не обещает цену или entitlement по одному имени модели.

## 9. Ежедневные продукты, игры и natal

**Есть:** Daily Crocodile/2048/trivia, briefings/reminders, process-specific
Crocodile models, hourly image quota и arbitrary-day preparation. Natal имеет
local PyEphem/equal-house расчёт, release fixtures и выключенный по умолчанию gate.

**DAILY-1 · P1 · M — Готовность и доставка по календарю.**

Свести scheduled/preparing/partial/ready/delivered/failed к понятному admin UI,
переиспользуя readiness. Проверить повтор после restart, unavailable judge/image,
границы UTC quota и локального дня. Разделить auto quota и explicit admin bypass.
**Приёмка:** retry не меняет прогресс/существующие слова, missing image не
блокирует разрешённую текстовую доставку, judge failure не расходует попытку;
статусы и расходы повторов видны оператору.
**Опора:** [runbook](docs/pollinations-daily-croc.md), [preparation](app/games/daily_preparation.py),
[tests](tests/test_daily_preparation.py), [quota tests](tests/test_daily_croc_quota.py).

**NATAL-1 · P1 для публичного запуска, иначе P2 · M/L — Release checklist.**

Выполнить существующие accuracy/readiness gates на целевом окружении, mobile/
desktop flows, city disambiguation и оценку интерпретаций при неизвестном
времени рождения. Уточнить share/expiry/deletion UX.
**Приёмка:** датированный отчёт по каждому пункту
[readiness](docs/natal-chart-product-readiness.md); сохранён equal-house scope,
нет обещания Swiss parity и передачи raw birth data по умолчанию. Новый движок/
city dataset — отдельное решение по пробелам, packaging, точности и условиям
распространения. Live smoke пишет отчёт: использовать согласованные тестовые данные.

## 10. Производительность и масштабирование

**Есть:** async runtime, DB pool, caches, Redis semaphores и локальный LRU state;
оптимизации JSON bytes, intent parsing и Damerau–Levenshtein уже реализованы.

**PERF-1 · P1 · M — Репрезентативная нагрузка.**

Разделить chat/research/image/Live/daily jobs; смешать нагрузки с fake providers,
затем ограниченно проверить реальные transport paths. Измерять p50/p95/p99,
loop lag, pool/semaphore wait, очередь и peak RSS. Выбрать regression threshold
по baseline, оптимизировать один подтверждённый bottleneck за раз.
**Приёмка:** сценарий с SHA/параметрами и сравнение до/после; улучшение не
ухудшает качество, fairness и cancellation.
**Опора:** [concurrency](app/adapters/concurrency.py), [state](app/state.py),
[database](app/database.py), [logging benchmark](scripts/benchmark_logging.py).
**Зависимость:** OBS-1, REL-1.

**SCALE-1 · P2 · XL — Владение состоянием между репликами.**

Перед второй активной репликой перечислить local locks/tasks, conversation state,
schedule ownership, leases, queue semantics и WebSocket affinity. Выбрать
закрепление за процессом, внешнее хранение или durable worker для каждого owner.
Не переносить весь `UserState` в Redis механически.
**Приёмка:** две реплики на isolated DB/Redis выдерживают failover, сохраняют
consent/provenance и не запускают jobs дважды в контролируемых сценариях;
crash uncertainty описана. **Зависимость:** REL-2, PERF-1, ADR и подтверждённая
нагрузка. Single-host остаётся допустимым вариантом до появления такой потребности.

## 11. Архитектура, конфигурация и работа разработчика

**Есть:** delivery/memory ADR, locked Python 3.14/uv, lint/type/test gates,
dependency audits и encoding checks. Mypy ещё не strict; handler overrides
отключают часть проверок. `.env.example` отсутствует.

**DX-1 · P0 · M — Контракт code → config → docs → deployment.**

Создать проверяемый реестр env/default/reader/required/secret/reload/forwarding.
Проверять `load_settings()` и infrastructure readers на fake values без живой
`.env`. Для legacy `USE_OPENROUTER`, `MAX_CONCURRENT_HEAVY_CALLBACKS` и
`ENABLE_PERSISTENT_QUEUE` отдельно решить: убрать обещание управления или
реализовать намеренный контракт. Добавить безопасный `.env.example` и setup.
**Приёмка:** каждый переключатель влияет на заявленный путь или помечен как
legacy/no-op; default drift ловится проверкой; новый GitHub Secret не считается
автоматически переданным в контейнер.
**Опора:** [config](app/config.py), [deploy](.github/workflows/deploy.yml),
[Compose](docker-compose.yml), [tests](tests/test_config_helpers.py).

**ARCH-1 · P1 · L — Углублять границы постепенно.**

Картировать изменения/зависимости в `app/web.py`, `app/web_miniapp.py` и `bot.py`;
выделять модули по domain flow, не по длине файла. Сохранить ingress/auth,
владельца final delivery и caller-owned memory transaction. Ужесточать типизацию
по одному boundary за итерацию.
**Приёмка:** contract tests сохраняют routes/guards/outcomes, модуль имеет
понятный API и меньше обходных зависимостей. Массовая перепись без пользы не нужна.
**Опора:** [ADR 0001](docs/adr/0001-single-owner-ai-response-delivery.md),
[ADR 0002](docs/adr/0002-provenance-safe-memory-graph-writes.md), [Mypy policy](pyproject.toml).

**DOC-1 · P1 · S — Предотвращать расхождение документации.**

Добавить проверку relative Markdown links/anchors и config defaults рядом с
encoding gate; обновлять current guide при изменении поведения. Исторические
планы и pass counts сохранять как историю.
**Приёмка:** намеренно сломанная ссылка/default ловится CI с точным файлом,
без зависимости от доступности внешнего сайта. **Зависимость:** DX-1.

## 12. Возможности за пределами ближайшего цикла

| Идея | Условие начала | Минимальный результат | Основной риск |
| --- | --- | --- | --- |
| Dedicated direct OpenAI provider | Подтверждённая потребность, бюджет и AI-1 | Typed adapter, capabilities, cancel/usage/fallback tests | Дополнительная поддержка без измеримого выигрыша |
| Multi-model synthesis/debate | Базовая модель проигрывает конкретный класс задач | Ограниченный эксперимент с оценкой качества, задержки и стоимости | Согласованная ошибка моделей и рост расходов |
| Общая групповая память | Согласованы участники, scope, consent/revoke и retention | Новая tenant/consent модель и изолированный пилот | Неявный сбор группы через private LTM недопустим |
| Квоты/тарифы как продукт | Есть подтверждённый коммерческий сценарий | Проверяемый usage ledger и прозрачные ограничения | Оценочная provider cost ещё не является биллингом |

Эти идеи не блокируют улучшение существующего бота. Внешний сервис, модель,
цены и условия проверяются отдельно на момент реализации.

## Последовательность поставки

Фазы зависят от результатов, не от календарных дат. Это порядок работы,
не обещание выполнить все категории одним исполнителем за квартал.

| Фаза | Состав | Условие выхода |
| --- | --- | --- |
| A. Установить факты | DX-1, OBS-1, SEC-1, первая выборка AI-1 | Однозначные settings, synthetic event найден, access matrix и baseline качества |
| B. Закрыть восстановление | REL-1, REL-2, AI-3, завершение AI-1 | Отказы/restore воспроизводимы, research имеет общий бюджет, качество/расходы сравнимы |
| C. Улучшить основной опыт | OBS-2, UX-1; затем один из RAG-1, MEM-1/MEM-2, MEDIA-1 | Измеримый выигрыш на выбранном сценарии без регрессии остальных |
| D. Поддержка и daily flows | PERF-1, DAILY-1, DOC-1, адресный ARCH-1 | Нагрузка, ежедневная готовность и документация проверяемы |
| E. Выбрать расширение | AI-2, SEC-2, NATAL-1, UX-2 или P2-эксперимент | Есть гипотеза, владелец, бюджет и отдельная приёмка |

Основные зависимости: `OBS-1 → OBS-2/PERF-1`, `AI-1 → AI-2/RAG-1`,
`MEM-1 → MEM-2`, `SEC-1 + REL-2 → SEC-2`,
`PERF-1 + REL-2 + ADR → SCALE-1`, `DX-1 → DOC-1`.
При наличии ресурсов независимые направления могут идти одновременно;
инициативы на одном boundary лучше выполнять последовательно.

## Первые небольшие поставки

1. **DX-1a:** env/default/forwarding registry и решение по legacy no-op;
   готовность — fake-env contract check и setup без изменения model defaults.
2. **OBS-1a:** один synthetic event проходит Docker → Alloy → Loki в CI;
   готовность — отключение collection приводит к диагностируемому fail.
3. **REL-1a:** timeout/partial stream с Telegram failure проверены до immutable
   outcome; готовность — повторяемые tests без реального provider.
4. **AI-1a:** первая ручная baseline-оценка chat/search/document Q&A с рубрикой;
   готовность — fixtures и отчёт без пользовательских секретов/текстов.
5. **REL-2a:** restore runbook и отдельное окружение; готовность — фактически
   выполненное восстановление с записанными ограничениями.

Для каждой поставки создавать отдельную задачу с владельцем, файлами, тестами
и критерием выхода. Выполнять focused checks, расширять по масштабу изменения
согласно [CONTRIBUTING](CONTRIBUTING.md). Integration, live provider и deployment
результаты отмечать отдельно. Пересматривать приоритеты после каждой фазы
по качеству, инцидентам, стоимости и обратной связи.
