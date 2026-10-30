# Daily Games and Administration Integrity Implementation Plan

> **Implementation record:** See the [dated implementation evidence](../../request-processing-dashboard-implementation-2026-10-28.md) and current runtime-controls guide. The original checklist below is retained as design history; its unchecked boxes do not establish current runtime behavior.

> **For agentic workers:** Use `executing-plans` task by task; `subagent-driven-development` applies only to explicitly delegated execution. These checkboxes describe proposed work.

**Goal:** Сделать Daily-игры неизменными для уже начавших играть пользователей, исключить stale генерации и потерю черновиков, превратить долгие административные действия в отслеживаемые jobs.

**Architecture:** Минимальное исправление 2048/Crocodile — CAS и запрет изменения игрового контента после появления results. Trivia сохраняет уже существующие immutable published revisions. Новые Daily jobs используют исправленное queue core и отдельный lane, но вызывают действующие domain services; HTTP и UI не пишут таблицы напрямую.

**Tech Stack:** Python 3.14, Quart, PostgreSQL/asyncpg, Redis, существующий JS/CSS, pytest и locked Chromium.

**Spec:** [Аудит](../../request-processing-dashboard-audit-2026-10-28.md), разделы 5–8; [master plan](2026-10-28-request-processing-and-dashboards.md); dependency [runtime R4](2026-10-28-request-runtime.md).

## Global Constraints

- Все ограничения master plan обязательны. Миграции только подготовить и проверить на изолированной БД; production применение не входит автоматически.
- Правила 2048 после первого result: immutable board, goal, spawn sequence, seed, par_moves, target_seconds, solution_moves. Статус доступности меняется отдельно с CAS, результаты не удаляются.
- Правила Crocodile после первого result на date/difficulty: не заменять слово, смысл/категорию, подсказки и игровую картинку; исправление готовить на новую дату. Операции доставки/служебные timestamps не относятся к игровому контенту.
- Внешние model/Telegram вызовы выполняются вне DB transaction. Порядок DB locks общий для edit/start: puzzle → result; дополнительные day locks согласуются с текущей дедупликацией слов.
- Даты Daily — `Europe/Kyiv`. Настройку личного времени доставки и существующие UTC/Pacific quota windows не переименовывать в Kyiv.
- Все admin mutations сохраняют существующие auth/CSRF/rate/body limits. Reason codes публичны; provider exception/body — не публичный JSON.

## Review Focus

- Start и edit одновременно, включая отсутствующую puzzle row — D1/D2.
- Lease истёк, reset выполнен, старый provider вернул результат — D2/D6.
- Auto/model сохранены из старой вкладки после изменения chain другим оператором — D3.
- Сохранение main, пока super dirty; 409/timeout/reload — D5/O4.
- Telegram принял сообщение без подтверждения; пользователь отписался/удалил аккаунт во время batch — D7.

## Карта файлов

| Файлы | Ответственность |
| --- | --- |
| `app/repos/daily_2048.py`, `app/games/daily_2048.py` | Допуск, неизменность правил, atomic start/edit |
| `app/repos/crocodile_daily.py`, `app/games/crocodile_daily.py` | Content revision и generation ownership, запрет замены сыгранного контента |
| `app/runtime_settings/processes.py`, `legacy_models.py` | Клиентская CAS для старых Daily selectors |
| `app/tarot_daily.py` | Prepare missing / force regeneration и effective model |
| Новые `app/games/admin_jobs.py`, `app/web_daily_jobs.py` | Allowlisted Daily job commands, preflight/status HTTP |
| Новые `app/repos/broadcast_jobs.py`, `app/broadcast_jobs.py` | Снимок получателей, исходы доставки, безопасное возобновление |
| `app/templates/admin_daily.html`; новые `app/static/js/daily-trivia-editor.js`, `daily-croc-editor.js`, `daily-jobs.js` | Состояние редакторов, безопасное отображение и восстановление job после reload |

Выносить только изменяемые секции. Не проводить одновременно полную декомпозицию `web.py` и всех 3000 строк Daily template.

### D1. 2048: статус, CAS и неизменные правила партии

**Findings:** D01/D02. **Файлы:** изменить `app/repos/daily_2048.py`, `app/games/daily_2048.py`, `app/handlers/daily_2048.py`, `app/web_miniapp.py`, `app/web.py`, `app/templates/admin_daily.html`; создать `scripts/migrations/075_daily_2048_edit_revision.sql`; расширить `tests/test_daily_2048.py`, `tests/test_daily_2048_ranked_integrity.py`, `tests/integration/contracts/test_daily_2048_postgres.py`.

**Контракт:** puzzle `revision: int >= 1`; create expected_revision=0, edit требует actual revision. Доменные ошибки `DailyPuzzleUnavailable(reason="draft"|"disabled")`, `DailyPuzzleConflict(actual_revision, reason="revision_conflict"|"puzzle_in_use")`; HTTP 409 с safe reason и актуальным публичным puzzle. `upsert_puzzle(..., expected_revision: int)` не имеет обхода проверки для admin.

- [ ] Добавить тесты: draft/disabled нельзя start ни Telegram, ни miniapp; disabled не принимает move и не меняет board/score. Повторный start ready сохраняет существующий result. `ensure_puzzle` не превращает намеренный draft/disabled в ready.
- [ ] Добавить migration revision с default для existing rows; не менять gameplay/results и не пересчитывать leaderboard. Обновить serializers и все callers CAS; missing revision от editor — 400, stale — 409.
- [ ] В start transaction заблокировать puzzle, проверить status и создать result в той же transaction. Для отсутствующей строки использовать conditional insert и повторное чтение под lock; scheduler `ensure` не перезаписывает опубликованную вручную строку.
- [ ] В edit transaction взять тот же puzzle lock, проверить revision и наличие любого result. При изменении gameplay и наличии result вернуть `puzzle_in_use`; save неизменных gameplay полей допустим. Статус disabled/ready — отдельная CAS mutation с сохранением gameplay и results.
- [ ] Добавить реальный PostgreSQL race test с двумя connections/barriers: если start победил, edit отклонён; если edit победил, result создан с целиком новыми правилами. Отдельно ensure-vs-admin insert: ручной пазл не затёрт. Не заменять доказательство SQL race Mock-объектом.
- [ ] В UI показать revision, played/locked и disabled reason; gameplay fields read-only после start, объяснение предлагает новую дату. Telegram и miniapp выводят понятное «игра недоступна», не stack trace.

```powershell
uv run --locked pytest tests/test_daily_2048.py tests/test_daily_2048_ranked_integrity.py tests/test_daily_2048_action_ownership.py tests/test_daily_2048_frontend.py --override-ini="addopts=" --timeout=30 -q
```

PostgreSQL contracts и migration contracts запустить по изолированному recipe. Готовность: ни один runtime path не использует mutable gameplay для уже начатой партии.

### D2. Crocodile: content revision, fencing генераций, запрет reset сыгранного

**Findings:** D03/D04. **Файлы:** `app/repos/crocodile_daily.py`, `app/games/crocodile_daily.py`, `app/games/daily_preparation.py`, `app/web.py`, `app/templates/admin_daily.html`; создать `scripts/migrations/076_daily_croc_generation_revision.sql`, `tests/test_daily_croc_generation_ownership.py`, `tests/integration/contracts/test_daily_croc_edit_races.py`; расширить `tests/test_daily_preparation.py`, `tests/test_daily_word_locking.py`.

**Контракт:** puzzle `content_revision: int`, `generation_token: UUID | None`. Content edit/reset увеличивает revision и инвалидирует token. Generation ticket содержит `(date, difficulty, expected_revision, token)`; setters `hints/prompt/asset/prepared` принимают ticket и возвращают applied/conflict, не делают unconditional UPDATE.

- [ ] Зафиксировать event-controlled regression: generator прочитал старое слово → await provider → reset → provider возвращает старые hints/prompt/image. Ни один старый setter не меняет новую строку, `prepared_at` не выставляется; job получает conflict/superseded.
- [ ] Добавить revision/token поля без уничтожения существующих puzzles/results. Implement `begin_preparation(date, difficulty, expected_revision) -> PreparationTicket` и `finish_preparation(ticket, assets)`: краткие transactions, no external I/O under lock. Ступенчатые setters тоже проверяют ticket; нельзя защитить только последний mark-prepared.
- [ ] Reset и ручные gameplay edits под общим puzzle row lock проверяют отсутствие results и expected revision. `get_or_create_result` использует тот же порядок lock, включая пути overview/completion summary. Старые результаты не удалять. UI текст о «сбросе прогресса» заменить точным описанием отказа при наличии игроков.
- [ ] Учесть существующую lazy preparation: создать нужный **текстовый** контент до первого result; если подготовка недоступна, вернуть retryable readiness, не создавать result с изменяемыми hints. Картинка остаётся optional при действующей quota/fallback политике; после первого result поздняя генерация не подменяет игровой контент. Служебные delivery message IDs можно обновлять отдельно.
- [ ] При ручной смене prompt/model для ещё не сыгранного пазла invalidation применяется к соответствующему generation ticket. Потеря Redis lease не разрешает запись в DB: DB CAS остаётся последней проверкой. Не увеличивать TTL как единственное исправление.
- [ ] Добавить PostgreSQL races start-vs-reset, edit-vs-finish, duplicate finish и два competing generations. Сохранить уникальность слов/difficulty rules, не удалять существующие locks до доказательства эквивалентности.
- [ ] Выполнить focused Crocodile/preparation tests и изолированные DB/Redis contracts; проверить short transaction duration без искусственных network sleeps под lock.

```powershell
uv run --locked pytest tests/test_daily_croc_generation_ownership.py tests/test_daily_preparation.py tests/test_daily_word_locking.py tests/test_daily_crocodile.py tests/test_daily_croc_quota.py --override-ini="addopts=" --timeout=30 -q
```

### D3. Сохранение model/prompt с корректной ревизией

**Findings:** D05/D06. **Зависимость:** D2 для puzzle CAS. **Файлы:** `app/repos/crocodile_daily.py`, `app/runtime_settings/processes.py`, `app/runtime_settings/legacy_models.py`, `app/web.py`, Daily JS; `tests/test_croc_admin_controls.py`, `tests/test_runtime_process_writers.py`, `tests/test_daily_text_model.py`; создать `tests/test_daily_settings_conflicts.py`.

**Контракты:** `set_puzzle_image_prompt(..., image_model: str | None = None, expected_revision: int)` — None сохраняет модель. Для browser writers `save_primary_model_at_revision(..., expected_revision: int, actor: str)` и `save_croc_model_at_revision(...)`; старые immediate-command wrappers отдельно получают свежую revision и вызывают тот же core.

- [ ] Тест prompt-only: выбранный nondefault image model остаётся прежним; explicit valid model меняется; invalid model отклоняется без частичной записи; revision увеличивается, старый generation token инвалидируется.
- [ ] Daily GET models/settings возвращает runtime settings revision и effective policy/source. POST требует `expected_revision` с той вкладки, где пользователь начал редактирование; не подставлять fresh revision на сервере вместо клиентской.
- [ ] Две вкладки: A загрузила Auto/old model, B установила primary+reserve, A сохраняет Auto. Ожидается 409 и сохранение chain B. Включить Trivia model, Croc shared и все role selectors; server-side effective chain должна остаться валидной.
- [ ] UI на 409 сохраняет локальный выбор, показывает актуальное значение/chain и предлагает явное обновление редактора; не повторяет POST автоматически. После успешного save обновляет revision во всех связанных формах через shared settings state.
- [ ] Сохранить immediate bot-команды и `runtime_settings_scope`: операция не меняет модель посередине при чужом save. Прогнать focused settings/croc controls tests и browser conflict test.

```powershell
uv run --locked pytest tests/test_daily_settings_conflicts.py tests/test_runtime_process_writers.py tests/test_croc_admin_controls.py tests/test_daily_text_model.py tests/test_runtime_operation_boundaries.py --override-ini="addopts=" --timeout=30 -q
```

### D4. Tarot: честные prepare missing и force regenerate

**Finding:** D07. **Файлы:** `app/tarot_daily.py`, `app/web.py`, Daily Tarot section; `tests/test_tarot_daily_precompute.py`; создать `tests/test_admin_tarot_regenerate.py`.

**Контракт:** admin operation `mode="missing"|"replace"`, `model: str | None`, target date и language. `missing` использует force=False; `replace` — force=True. Явно выбранная модель является override **только этой операции**; если model=None, используется текущая process policy. Не менять глобальный process для разовой генерации.

- [ ] Регрессия: все 156 синтетических variants существуют; missing → 0 generated/156 skipped; replace → вызовы генерации и перезапись выбранных variants. Проверять фактические provider model arguments, не только JSON запроса.
- [ ] Провалидировать model по подходящему catalog/capability. Для explicit override построить однозначный operation model plan из выбранной модели; key rotation сохраняется, скрытая подмена process chain исключена. Для Auto сохранить существующие chain/fallback и записать actual model каждого успешного результата.
- [ ] Разделить две кнопки и confirmation с датой, количеством variants, effective model и mode. Не показывать success как «перегенерировано», если generated=0 или locked=true. Summary показывает generated/skipped/failed/locked отдельно.
- [ ] Проверить частичный provider failure, конфликт одновременных запусков и отсутствие API calls при невалидном input. Долгий HTTP пока не считать окончательно исправленным: endpoint переводится в job в D6.
- [ ] Выполнить targeted tests:

```powershell
uv run --locked pytest tests/test_admin_tarot_regenerate.py tests/test_tarot_daily_precompute.py tests/test_tarot_daily_handler.py --override-ini="addopts=" --timeout=30 -q
```

### D5. Независимые Trivia drafts и безопасный Crocodile DOM

**Findings:** D08/D09. **Файлы:** `app/templates/admin_daily.html`; новые `app/static/js/daily-trivia-editor.js`, `app/static/js/daily-croc-editor.js`; создать `tests/test_admin_daily_editor_browser.py`; расширить `tests/test_admin_daily_browser_security.py`, `tests/test_admin_dailycroc_csp.py`.

**Контракт editor state:** `{date, baseRevision, baseMain, baseSuper, draftMain, draftSuper, mainDirty, superDirty, conflict}`. DOM — представление этого состояния, не единственное место хранения текста. Черновики живут в памяти вкладки; persistent storage для их текста не добавлять.

- [ ] Сначала Chromium regressions под реальным CSP: редактировать super → save main → super неизменён и dirty; обратная операция аналогично. На 409, reload списка, regenerate соседней части и смену даты черновик не исчезает без явного выбора пользователя.
- [ ] Save отправляет один section с base revision. Успех обновляет base revision и сохранённую section, сохраняя соседний draft. Если API возвращает весь puzzle, не перезаписывать dirty sibling. При обнаружении внешней смены общего revision не выполнять скрытый merge — показывать conflict и сравнение.
- [ ] Ввести явные действия «Загрузить серверную версию» и «Продолжить редактировать»; при dirty смене даты или regeneration affected section запросить выбор. После отмены выбора не менять selection и focus. Оба drafts восстанавливаются в DOM из state.
- [ ] Croc template создавать из статической разметки; image_prompt/word/topic/model назначать через `textContent`/`.value`, image URL только через существующий проверенный proxy path. Тестовые markup/textarea delimiters отображаются буквально и не создают дополнительные элементы/attributes.
- [ ] Сохранить CSP, cookie auth и sanitization остальных секций. Проверить, что загрузка extracted JS разрешена текущей политикой без `unsafe-inline` ослабления.
- [ ] Пройти browser suite и существующие JS tests:

```powershell
$env:GEMAIBOT_REQUIRE_BROWSER_TESTS = "1"
uv run --locked pytest tests/test_admin_daily_editor_browser.py tests/test_admin_daily_browser_security.py tests/test_admin_dailycroc_csp.py -m browser --override-ini="addopts=" --timeout=30 -q
uv run --locked pytest tests/test_daily_admin_errors_js.py tests/test_daily_croc_admin_js.py --override-ini="addopts=" --timeout=30 -q
```

### D6. Ограниченные Daily jobs, preflight и календарь готовности

**Finding:** D10; дополнение продукта. **Зависимости:** runtime R4a–R4d, D1–D4. **Файлы:** новые `app/games/admin_jobs.py`, `app/web_daily_jobs.py`, `app/static/js/daily-jobs.js`, `scripts/migrations/077_daily_tarot_generation.sql`, `tests/test_daily_admin_jobs.py`, `tests/test_daily_jobs_browser.py`; изменить `app/games/daily_preparation.py`, `app/web.py`, `app/tarot_daily.py`, `app/templates/admin_daily.html` и domain authoring callers.

**HTTP-контракт:**

```text
POST /api/admin/daily/jobs/preview
  {kind, date, difficulty?, part?, mode?, model?, expected_revision?}
  -> {scope, blocked_reason?, work_items, effective_policy, readiness}
POST /api/admin/daily/jobs
  {operation_id, ...same command}
  -> 202 {job_id, state, status_url, storage_mode}
GET /api/admin/daily/jobs/<job_id>
  -> {state, progress, generated, skipped, failed, reason_code, effective_revision}
GET /api/admin/daily/readiness?start=YYYY-MM-DD&days=8
  -> {timezone, today, days: [...], observed_at}
```

Allowlist kinds: `croc.prepare_day`, `croc.regenerate_image`, `croc.reset_word`, `trivia.regenerate`, `trivia.validate_save`, `tarot.prepare`. Payload для Trivia draft ограничен текущим body cap, не выводится в status/log. Simple no-AI saves остаются короткими CAS HTTP.

- [ ] Тест admission: повтор operation_id возвращает тот же job, очередь заполнена → 429, backend admission unavailable → 503/unknown reconciliation по контракту R4a; не возвращать 202, если TaskManager отклонил coroutine. Сохранить существующую day preparation дедупликацию и read-only readiness.
- [ ] Зарегистрировать handlers в daily lane: стартовые 32 nonterminal jobs, 256 KiB/job, 8 MiB total, 2 workers; image permit=1. Queue age deadline=30 min, execution timeout=10 min для Croc/Trivia и 30 min для Tarot; values централизованы в policy и проверены fake clock. Полный Tarot может превысить бюджет реальной модели: вернуть partial со счётчиками, затем безопасное missing-resume, не обещать завершение всех variants.
- [ ] Сопоставить результаты с общим JobState: полная подготовка → succeeded; неполная → failed с reason_code=partial_generation и generated/skipped/failed counters; superseded revision → failed с reason_code=revision_conflict. Не вводить несовместимый state=partial только для новой очереди. Старый preparation endpoint при необходимости переводит это в прежний response contract. Повтор частичной подготовки использует missing mode, а replace требует нового явного действия.
- [ ] Conflict key: Croc `date+difficulty` (prepare_day резервирует обе сложности в стабильном порядке), Trivia `date`, Tarot `date+language`. Два разных operation ID по одному conflict key не запускаются одновременно; ответ содержит существующий job. Reservation и release owner-fenced; DB revision всё равно проверяется при commit результата.
- [ ] Закреплять fresh runtime settings snapshot на **начало execution** и сохранять effective revision/model plan в metadata; внутри операции все стадии используют его. Preview указывает, что Auto policy может измениться до start. Explicit model из D4 — параметр job и не меняется.
- [ ] При отмене/timeout/lease loss прекращать owned provider tasks и запрещать позднюю запись через revision/token D2/Trivia CAS. В migration 077 для Tarot добавить таблицу generation ownership с ключом date/language, token и revision; перед upsert каждой variant в той же короткой DB transaction проверять current token под row lock. Начало новой генерации меняет token; завершение старого owner не освобождает новый. Existing day-preparation statuses приводятся к общему протоколу без потери active job.
- [ ] Переделать длительные кнопки на enqueue + status polling с bounded backoff. После reload восстанавливать job по date/kind или operation ID; в session storage допустим только ID и фильтр, не текст draft/token/credentials. Показывать progress по реальным completed work_items, не по таймеру.
- [ ] Добавить календарь сегодня + 7 дат с Easy/Hard/2048/Trivia/Tarot readiness и ссылкой на job. GET/readiness не генерирует; перед подготовкой диапазона preview показывает missing assets, количество jobs и блокировки сыгранного контента. Первоначально запускать одну выбранную дату за действие, не делать автоматическую массовую платную генерацию.
- [ ] Проверить polling закрытой/потерявшей сеть вкладки, повтор POST, restart worker, stale owner, частичный результат и cancel-before-start. Прогнать unit + browser; реальный Redis recovery и PostgreSQL generation-token contracts — в isolation.

```powershell
uv run --locked pytest tests/test_daily_admin_jobs.py tests/test_daily_preparation.py tests/test_tarot_daily_precompute.py --override-ini="addopts=" --timeout=30 -q
$env:GEMAIBOT_REQUIRE_BROWSER_TESTS = "1"
uv run --locked pytest tests/test_daily_jobs_browser.py -m browser --override-ini="addopts=" --timeout=30 -q
```

### D7. Журнал broadcast и управляемая batch-доставка

**Finding:** D11. **Зависимость:** D6 и runtime R4. **Файлы:** создать `scripts/migrations/078_broadcast_delivery_journal.sql`, `app/repos/broadcast_jobs.py`, `app/broadcast_jobs.py`, `tests/test_broadcast_job_recovery.py`, `tests/integration/contracts/test_broadcast_journal.py`; изменить `app/web.py`, `app/templates/admin_daily.html`, `app/repos/users.py`, `app/handlers/daily_crocodile.py`, `app/handlers/daily_2048.py`, `app/handlers/daily_trivia.py`, `app/handlers/scheduled_horoscopes.py`, `app/handlers/tarot_daily.py` и вызываемый ими sender owner; расширить `tests/test_admin_daily_broadcast.py`.

**Данные:** batch metadata + recipients snapshot + delivery attempts. Уникальный `(batch_id, recipient_id, channel)`; attempt содержит operation_id, timestamps, outcome (`sent|failed|skipped|unknown_delivery`), allowlisted reason, telegram message_id при подтверждении, effective day/model revision. Не хранить полный отправленный текст, username, raw response или токен. Индексы по `(batch_id, state)`, `(channel, created_at)` и job ID; RLS/privileges по текущему repository pattern. Retention 30 дней для персонального delivery journal; account erasure удаляет связанные строки раньше.

- [ ] Добавить migration/erasure tests: новые таблицы закрыты от обычного публичного доступа, account deletion удаляет recipient/attempt records, batch aggregate остаётся обезличенным. Existing migrations не переписывать.
- [ ] Разделить preview и enqueue. Preview возвращает фактическое число получателей и причины exclusions; enqueue фиксирует набор ≤100, текущую верхнюю границу batch, с operation ID. Перед **каждой** отправкой повторно проверить consent/subscription/account generation; снятие подписки после preview → skipped.
- [ ] Использовать отдельный `broadcast` lane с 1 batch worker и до 5 concurrent recipient sends (текущая степень batch parallelism), max 10 nonterminal batches. Daily image/Tarot worker pool не расходуется на Telegram batch. Reuse существующий delivery owner и RetryAfter handling, не отправлять напрямую второй копией из HTTP.
- [ ] Перед внешней отправкой записать attempt-started; подтверждённый Telegram response → sent; definite reject → failed/skipped; transport timeout после возможной передачи → unknown. Crash с unresolved attempt после restart также unknown: без receipts нельзя считать его безопасно неотправленным.
- [ ] Реализовать `retry failed`: новый batch только для определённо retryable failed recipients, новый parent ID, повторная проверка согласия. Уже sent и unknown исключаются. Двойное нажатие с тем же operation ID не создаёт второй batch.
- [ ] Заменить stub `/api/admin/broadcast/errors` реальной пагинацией journal с фильтрами channel/date/reason/state; на отсутствие данных/истории явно указать coverage start. Заполнить Tarot delivery counters только из наблюдённых записей, не задним числом из предположений.
- [ ] Проверить partial send, 429/RetryAfter, blocked user, unsubscribe между preview и send, account erase, timeout-after-send, restart-after-send-before-commit. Проверять число вызовов fake Telegram и неизменность sent entries, а не только HTTP 200.
- [ ] Выполнить focused broadcast/erasure tests и PostgreSQL contracts; обновить runbook с точным значением unknown и правилами ручного расследования без вывода приватных текстов.

```powershell
uv run --locked pytest tests/test_broadcast_job_recovery.py tests/test_admin_daily_broadcast.py tests/test_daily_unreachable_delivery.py --override-ini="addopts=" --timeout=30 -q
```

**Выход Daily-плана:** администратор видит, что именно будет изменено, не может нарушить уже начатую игру, сохраняет черновики и получает проверяемый статус долгой операции. Ни одна кнопка не обещает отсутствие дубля внешней отправки при неизвестном исходе.
