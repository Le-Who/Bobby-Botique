# Аудит runtime, ошибок и дэшборда — 2026-10-23

Исходная точка: `25e25897`, ветка `vps_testai`. Это отчёт о проверке исходников
и локальных offline-сценариях, а не свидетельство работы production VPS.
Промпты, модели и голоса разобраны отдельно в
[инвентаре промптов](prompt-quality-audit-2026-10-23.md).
Текущие эксплуатационные контракты находятся в
[runtime controls](runtime-controls.md), [logging](logging.md) и
[CONTRIBUTING](../CONTRIBUTING.md).

## Способ проверки

Работа разделена между тремя субагентами: prompts/TTS и runtime получили
reasoning `xhigh`, dashboard — `high`. Координатор проверял кэш, метрики,
документы и интеграцию. Dashboard-агент дополнительно независимо проверил
runtime, prompts/TTS и изменения координатора; его замечания исправлены
до финального общего прогона.

Синтаксический обход охватил все Python runtime-файлы в `app/` и `bot.py`:
на момент обхода 290 файлов, 103 414 строк и 1613 async-функций. Проверка
искала известные синхронные сетевые, subprocess и sleep-вызовы непосредственно
в async-функциях; таких совпадений не обнаружено. Это узкая статическая
проверка, она не измеряет CPU-стоимость произвольного вызова или latency сети.
Найденный отдельно блокирующий `psutil.cpu_percent(interval=0.1)` исправлен.

| Область | Подробно проверенные границы |
| --- | --- |
| Модели и инструкции | Определения и реальные consumers, прямые SDK-вызовы, thinking/sampling, tool responses, token budget, TTS profiles; отдельный полный инвентарь |
| Runtime | Ingress, авторизация альбомов, request-owned/background задачи, provider single/key-race/model-hedge, retries, ожидаемые transport/API errors, alerts, shutdown |
| Dashboard | HTTP snapshots, SSE, очереди/метрики, polling, ошибки Daily Admin, controls CAS/drafts и голосовые поля, authentication и offline Chromium |
| Хранение и контекст | Search cache, Redis namespaces, дневные/пользовательские метрики, state persistence barrier, документные лимиты, UTF-8 budget памяти |

Проверка game algorithms, внешних Live-сессий и всех вариантов каждого
domain handler опирается на существующий offline-набор, а не на ручное
воспроизведение каждой команды. Нет утверждения об отсутствии всех возможных
дефектов или о полной проверке внешних сервисов.

## Подтверждённые исправления

### Ошибки, повторные запросы и фоновые задачи

- Общий лимит incident/unauthorized alerts резервируется до отправки. Параллельные
  отказы и неуспешный transport больше не обходят ограничение; per-user cooldown
  также резервируется до await. Существующая очистка секретов сохранена.
- Async callback ошибки принадлежит исходной фоновой задаче; его исключение
  регистрируется, а shutdown дожидается его вместе с задачей. Drain повторно
  учитывает принятые дочерние работы в пределах общего срока.
- Router различает запрос без возможности повтора, повтор позже, следующий ключ
  и следующую модель. Неправильный payload прекращает лишние обращения; отказ
  доступа к одной модели сохраняет возможность перейти к следующей. Ошибка
  с `KeyDisposition.UNCHANGED` не ухудшает здоровье исправного ключа.
- Typed HTTPX/Telegram timeout, network, rate-limit и HTTP status не теряются
  при пустом сообщении исключения. В native Gemini отдельно проверены пустые
  HTTP 429, HTTP 504, authentication и `ReadTimeout`.
- Глобальный Telegram owner сохраняет первичное событие с `error_code` и безопасный
  ответ пользователю. Ожидаемые network/timeout/limit/input/document ошибки
  регистрируются как warning без incident alert; исчерпание квот и key/config
  проблемы требуют warning администратору, неожиданные ошибки — critical с `error_id`.
- Фотоальбомы проходят admission после авторизации и rate limit. Синтетический
  Update одиночного фото имеет `effective_message`; обработка использует
  пользовательскую блокировку. Альбомы и их очистка имеют явных владельцев.
- Остановка ingress/производителей и задач предшествует финальной записи state
  и memory и закрытию transport. Принятые перед остановкой работы учитываются
  и в обычной, и в аварийной ветви shutdown.
- Network retry helpers передают аргументы функции и создают полный HTTPX Timeout;
  прежний ложный отрицательный результат проверки соединения устранён.

### Кэш и метрики

- `/clearcache` удаляет только поисковые `qna:<sha256>` и `search:<sha256>` ключи
  через SCAN и ограниченные DELETE-пакеты. `FLUSHDB` удалён: очереди, Reader,
  deduplication и coordination keys сохраняются. Все локальные cache tiers
  очищаются и без Redis; ошибка backend доходит до результата команды.
- Memory cache hits учитываются в hit rate. Необъектный JSON из Redis считается
  cache miss и не попадает в типизированный поисковый ответ.
- API-only и cache-only интервалы сохраняются без требования завершённого запроса.
  Пользовательские model events также не требуют request counter в том же интервале.
- При известном частичном успехе повторно ставятся в очередь только ещё не
  сохранённые счётчики. Уже записанный общий итог не прибавляется ещё раз из-за
  последующего отказа пользовательского batch.
- Исторические DB totals отделены от новых deltas: restart не переписывает их как
  новые события, а периодический flush не обнуляет дневную сводку. Остаток прошлого
  дня сохраняется за исходную дату. Отмена flush восстанавливает pending counters;
  cleanup сначала обрабатывает уже принятые события очереди.

Неизвестный результат commit при разрыве соединения остаётся границей additive
persistence: offline-тест не доказывает exactly-once после потери подтверждения БД.
Для такой гарантии нужен отдельный durable idempotency contract.

### Документы и дэшборд

- Лимит обработчика документов 10 MB применяется до download при известном размере
  Telegram. Прежние transport limits 50 MB / 2 GB больше не вызывают заведомо
  бесполезную передачу. Текст ошибки показывает фактический processing limit.
- DOCX paragraphs/tables извлекаются до лимита 100 000 символов; PDF также не
  превышает этот предел на одной большой странице и учитывает разделители.
  `truncated` передаётся до пользовательского сообщения, ограничение не скрывается.
- SSE использует действующие `pending_tasks`, `running_tasks` и process request/error
  counters вместо устаревших полей. Системные замеры выполняются в рабочем потоке.
- Poll допускает один активный запрос, отменяет его через 10 секунд и корректно
  восстанавливается после ошибки. Critical/warning classes не накапливаются.
- Daily Admin выводит toast и сообщения ошибок как экранированный текст.
  Ответ провайдера/сервера не интерпретируется как HTML админской страницы.
- Voice controls сохраняют optional индивидуальный стиль/legacy temperature
  при refresh, перестановке и сохранении; пустое поле удаляет только этот override.

## Границы подтверждения

Все новые сценарии используют синтетические данные, mock HTTP/Telegram edges,
offline Quart и локальный Chromium. Настоящие `.env`, пользовательские сообщения,
приватные логи, birth data и DB dumps не читались. Миграции и production-сервисы
не запускались; dependency graph не менялся.

Проверка кода не читает и не перезаписывает сохранённые в production overrides.
Новые baseline-инструкции применяются там, где нет явного override; инструкция,
выбранный голос и маршрут уже начатой операции сохраняют свой снимок ревизии.
Качество произношения и реальная provider latency требуют live-прослушивания
и замеров; offline-проверка подтверждает payload и владение ресурсами.

## Проверки итогового дерева

Среда: Python 3.14.3, uv 0.13.5, Node.js 24.13.0. Установленный общий `uv`
имел версию 0.12.6, поэтому точный закреплённый runner запускался изолированно
через `uv tool run --from 'uv==0.13.5' uv run --locked …`. Manifests и оба
dependency lock-файла остались без изменений.

| Проверка | Результат |
| --- | --- |
| `ruff check .` | PASS |
| `ruff format --check .` | PASS, 764 файла |
| `mypy app bot.py` | PASS, 290 source-файлов; существующие annotation-unchecked notes не скрывались |
| `python -X utf8 scripts/check_encoding.py` | PASS, 95 Markdown-файлов |
| `python -X utf8 scripts/check_docs_links.py` | PASS, 35 current guides, 0 ошибок |
| Отдельный `check_document` для двух новых датированных audit reports | PASS, 0 ошибок |
| `python -X utf8 scripts/check_env_registry.py` | PASS, 111 env names, 0 изменений |
| `git diff --check` | PASS |

До общего прогона прошли самостоятельные затронутые наборы: prompts/TTS —
313 tests, runtime — 422 tests, dashboard — 117 tests с тремя реальными Chromium
сценариями, cache/metrics/documents — 94 tests. Эти наборы пересекаются; их
числа не складываются. Независимые review-прогоны также не являются отдельным
числом уникальных тестов.

Первая попытка общего прогона выполнялась во время изменений исходников:
5008 passed, один source-inspection failure в `test_rls_policies.py`
(`key-health-endpoint`). Изолированный повтор прошёл, что согласуется со сдвигом
строк при уже импортированном коде. Такой mutable run не использовался как
финальное подтверждение. При запуске окончательных gates дополнительно
исправлено только форматирование нового теста; начатый прогон остановлен и
запущен заново до любых утверждений об успешном результате.

Следующий последовательный прогон неизменяемого runtime дал 5131 passed,
7 failed, 7 deselected за 515.23 s. Все семь failures оказались прежними
test expectations: три маленьких бюджета memory на 3.8, sampler в brief,
начало Tarot до общей рамки и два старых shutdown-order expectations.
Проверки обновлены с сохранением содержательных ограничений: модельные
параметры отдельно проверяются на 3.8, 3.5 Lite и legacy 2.5; Tarot сравнивает
полную управляемую композицию и одноразовую подстановку; shutdown сохраняет
полную проверку порядка ресурсов. Runtime source SHA-256 до и после этих правок
одинаков: `cbd1877605969859c827e02518ced24633b1cf1c94e68cf3cfeeed466f46763c`.

**Окончательный полный offline-прогон: 5146 passed, 0 failed за 188.30 s.**
Использован штатный `loadgroup` с четырьмя workers и новым отдельным временным
каталогом; исходники и тесты во время этого прогона не изменялись.
`GEMAIBOT_REQUIRE_BROWSER_TESTS=1` включён. Integration-маркер и каталог
`tests/integration` исключены намеренно: disposable PostgreSQL/Redis не поднимались,
и прохождение offline-набора не объявляется результатом интеграционных проверок.

Воспроизводимая команда из PowerShell:

```powershell
$env:PYTHONUTF8 = '1'
$env:GEMAIBOT_REQUIRE_BROWSER_TESTS = '1'
$goalTestTemp = Join-Path ([IO.Path]::GetTempPath()) ('gemaibot-audit-' + [guid]::NewGuid().ToString('N'))
uv tool run --from 'uv==0.13.5' uv run --locked pytest tests/ `
  --ignore=tests/integration -m 'not integration' -n 4 --dist=loadgroup `
  --basetemp $goalTestTemp --override-ini='addopts=' --timeout=30 -q
```
