# Operator Dashboards and Diagnostics Implementation Plan

> **Implementation record:** See the [dated implementation evidence](../../request-processing-dashboard-implementation-2026-10-28.md) and current metrics/runtime-controls guides. The original checklist below is retained as design history; its unchecked boxes do not establish current runtime behavior.

> **For agentic workers:** Use `executing-plans` task by task; `subagent-driven-development` only for explicitly delegated execution. These are proposed steps, not completed functionality.

**Goal:** Показать реальные очереди и причины ожидания, сократить нагрузку самого мониторинга, сделать админские формы предсказуемыми и проверять качество маршрутизации запросов.

**Architecture:** Метрики строятся из существующих owner events и bounded aggregates. Backend разделяет сбор данных и доставку snapshot/SSE. UI остаётся на существующем JS/CSS; queue actions используют проверенный runtime contract, не внутренние Python task objects.

**Tech Stack:** Python 3.14, Quart, existing metrics/events, Redis job store, vanilla JS/CSS, pytest и locked Playwright/Chromium.

**Spec:** [Аудит](../../request-processing-dashboard-audit-2026-10-28.md), разделы 5, 7–8; [master plan](2026-10-28-request-processing-and-dashboards.md); dependencies [runtime](2026-10-28-request-runtime.md), [Daily](2026-10-28-daily-integrity.md).

## Global Constraints

- Все ограничения master plan обязательны. Existing user/API accounting и Kyiv periods не пересчитывать новой эвристикой.
- Не писать новые raw logs и не собирать production prompts ради графиков. Cardinality labels ограничена allowlists; request/user/job ID не является metric label.
- Unknown/unavailable/degraded — отдельные значения. Нельзя показывать нулевую очередь или нормальный bot status только потому, что HTTP отвечает.
- Сохранить CSP/auth/CSRF, optimistic settings revision, текстовый DOM и per-operation snapshots.
- Никаких массовых destructive actions, произвольных runtime concurrency sliders и редактирования provider secrets из новой панели.

## Review Focus

- Медленный источник и 20 одновременно открытых вкладок: один expensive refresh, независимая доступность источников — O2.
- Частичный поток: first visible text и terminal outcome считаются один раз — O1.
- Cancel/finish одновременно; stale revision админской команды: состояние не откатывается — O3.
- HTTP POST timeout и поздний ответ GET после смены формы: нет скрытого повторения и потери draft — O4.
- Цитата/отрицание/продолжение истории/явный thinking off: не ошибочный shortcut из одного ключевого слова — O5.

## Карта файлов

| Файл | Назначение |
| --- | --- |
| `app/observability/metrics.py`, текущие emit owners | Ограниченные histograms, этапы, правильный logical request outcome |
| Новый `app/dashboard_sources.py` | Shared snapshot cache, single-flight, freshness и lifecycle readiness |
| `app/web.py`, `app/templates/dashboard.html` | Snapshot/SSE и отображение источников |
| `app/web_controls.py`, новый `app/static/js/controls-queues.js` | Read-only очередь и allowlisted CAS actions |
| Новый `app/static/js/admin-api.js` | Bounded reads/short mutations, typed HTTP errors, AbortSignal |
| Новый `tests/fixtures/intent_routing_cases.json` | Синтетический версионируемый corpus intent/thinking |

### O1. Метрики этапов и всех видов нагрузки

**Findings:** O02/R02. **Зависимость:** R1–R3; queue state из R4 подключить после готовности. **Файлы:** `app/observability/metrics.py`, `app/observability/events.py`, `app/update_processor.py`, `app/adapters/concurrency.py`, `app/providers/router.py`, `app/response_delivery/delivery.py`, `app/queue.py`, `app/voice_engine.py`, `docs/log-events.md`; расширить `tests/observability/test_operational_metrics.py`, `tests/test_metrics_accounting.py`; создать `tests/test_request_stage_metrics.py`.

**Контракт:** фиксированные stage values `ingress_wait`, `admission_wait`, `context`, `first_visible_text`, `generation`, `delivery`, `total`; workload allowlist из текущей доменной модели. Total отсчитывается от принятия логической пользовательской операции до единственного terminal; PTB dispatch duration не подменяет detached generation duration. First-visible-text emitted один раз delivery owner, provider TTFT остаётся отдельной метрикой попытки.

- [ ] Переиспользовать существующие events и добавить недостающие owner milestones. Controlled-clock тест: queue wait + context + provider + delivery попадают в свои stage; cancel/reject/partial имеют отдельный outcome. Не суммировать update/http/request события в один счётчик пользовательских запросов.
- [ ] Добавить bounded histogram с buckets seconds `[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 900, +Inf]`, count/sum. Для панели окно 15 минут из 15 одноминутных сегментов; label combinations ограничены текущим max-pairs cap, лишнее → other. Prometheus cumulative histogram сохраняет стандартную семантику.
- [ ] Вычислять приближённые p50/p95/p99 по buckets, показывать sample count/window и low sample при n<20. Не складывать p95 стадий для total p95. Перцентили одной replica не выдавать за весь cluster.
- [ ] Добавить gauges/snapshot для ingress buffered/outstanding, TaskManager accepted/running/rejected, heavy/ultra waiters/mode, TTS jobs/bytes, queue lanes и Daily jobs. Каждый source возвращает scope, observed_at и available; без текстов/истории.
- [ ] Добавить amplification API attempts / accepted generation requests за совместимое окно, winner/cancelled-before-text/late-loser counts по model/workload. Различать теоретическую concurrency и фактическую среднюю; pure tool intents не включать в знаменатель генераций.
- [ ] Проверить cardinality cap, rollover, отсутствие ID/prompt в export, timeout/cancel/zero samples. Обновить event catalog и metrics guide у владельцев фактов.

```powershell
uv run --locked pytest tests/test_request_stage_metrics.py tests/observability/test_operational_metrics.py tests/test_metrics_accounting.py tests/test_metrics_traffic.py --override-ini="addopts=" --timeout=30 -q
```

### O2. Общий snapshot cache и честное состояние источников

**Findings:** O01/O02. **Файлы:** создать `app/dashboard_sources.py`, `tests/test_dashboard_source_cache.py`; изменить `app/web.py`, `bot.py`, `app/templates/dashboard.html`; расширить `tests/test_dashboard_snapshot.py`, `tests/test_dashboard_backend.py`, `tests/test_dashboard_runtime.py`, `tests/test_dashboard_diagnostics_browser.py`.

**Контракт:** `SourceSnapshot(value, observed_at, refresh_started_at, available, stale, reason_code, scope)`; `DashboardSourceCache.get(name, *, max_age_seconds)`. Single-flight на process/source/authorization scope, без общего cache персональных значений. Lifecycle `starting|ready|draining|stopped|failed` выставляет фактический bot supervisor.

- [ ] Test 20 concurrent snapshot/SSE readers: за TTL один expensive traffic query; падающий source не отменяет другие. Cancel reader не отменяет shared refresh; shutdown завершает owned refresh tasks.
- [ ] Начальные TTL: traffic 15 s, DB/service health 15 s, queue/system 5 s. Refresh сохраняет existing per-source timeout ≤5 s. Last-known возвращается со stale/observed_at; после 60 s без успешного refresh — unavailable с timestamp последнего наблюдения.
- [ ] SSE читает общий cache раз в 5 s, передаёт версии/лёгкие поля и не запускает SQL для каждого connection. HTTP snapshot читает тот же cache. Скрытая вкладка прекращает polling; возврат visibility делает один запрос без fan-out.
- [ ] `_flush_lock` traffic accounting не убирать. Cache хранит атомарный read result; pending deltas/coverage не складываются повторно. Settings saves и queue actions инвалидируют только свои sources, burst объединяется single-flight.
- [ ] Заменить hardcoded running lifecycle source. HTTP alive, bot ready, DB health и queue owner health — отдельные поля. LIVE означает связь; cards имеют age/degraded. Startup/drain/failure тест не может получить ready только из-за живого Quart.
- [ ] Chromium: отказ одного source сохраняет остальные данные, stale помечен, unavailable не становится 0. Счётчиком fake DB calls подтвердить переход от зависимости от числа вкладок к TTL; не обещать абсолютное ускорение production.

```powershell
uv run --locked pytest tests/test_dashboard_source_cache.py tests/test_dashboard_snapshot.py tests/test_dashboard_backend.py tests/test_dashboard_runtime.py --override-ini="addopts=" --timeout=30 -q
$env:GEMAIBOT_REQUIRE_BROWSER_TESTS = "1"
uv run --locked pytest tests/test_dashboard_diagnostics_browser.py -m browser --override-ini="addopts=" --timeout=30 -q
```

### O3. Очереди и контролируемые действия администратора

**Findings:** Q05/O02; новое продуктовое управление. **Зависимости:** R4a–R4d, O1/O2; Daily/broadcast — D6/D7. **Файлы:** `app/web_controls.py`, `app/templates/controls.html`, новый `app/static/js/controls-queues.js`, `app/static/css/controls.css`; создать `tests/test_queue_controls.py`, `tests/test_queue_controls_browser.py`.

**API под существующим controls auth/CSRF:**

```text
GET  /api/controls/queues
GET  /api/controls/jobs?lane=...&state=...&cursor=...&limit=50
GET  /api/controls/jobs/<id>
POST /api/controls/jobs/<id>/cancel {expected_revision, operation_id}
POST /api/controls/jobs/<id>/retry  {expected_revision, operation_id}
POST /api/controls/queues/<lane>/state
     {expected_revision, operation_id, admission_paused, claim_paused}
```

Ответ содержит capabilities, revision, scope/freshness и safe reason. Policy limits read-only. PTB, TaskManager и TTS сначала показывают gauges без actions, если их owner не реализует соответствующий command contract.

- [ ] Сначала вкладка «Очереди» read-only: cards accepted/running/delayed/retrying/oldest age/capacity/bytes, jobs kind/date/age/attempts/state/actual model revision. Cursor pagination ≤100, без payload dump. Поиск job ID разрешён администратору, raw provider request не показывать.
- [ ] Тест auth/CSRF, invalid lane/state/cursor/limit, отказ store. Один failed source не скрывает остальные; instance-local маркируется, общие Redis counters не суммируются по replica.
- [ ] Actions подключить после R4b tests. Cancel остаётся cancel_requested до подтверждения owner. Retry создаёт новый job только для разрешённого failed/expired; sent и unknown_delivery не получают кнопку retry.
- [ ] Mutation требует expected_revision и operation_id; double-click возвращает прежний result. Race cancel-vs-finish → 409 или актуальный terminal без отката. Pause admission, pause execution и drain объяснять отдельно; confirmation содержит lane, pending/running и поведение уже запущенных tasks.
- [ ] Admin command event включает actor reference по privacy policy, action/target/previous-new revision/reason, без payload. Toast не заменяет подтверждённый backend state.
- [ ] Browser: unknown без retry; stale revision сохраняет открытый job; после refresh backend и UI совпадают; cancel_requested отличается от cancelled. Mobile table может иметь внутренний scroll, страница не переполняется.

```powershell
uv run --locked pytest tests/test_queue_controls.py tests/test_web_controls.py --override-ini="addopts=" --timeout=30 -q
$env:GEMAIBOT_REQUIRE_BROWSER_TESTS = "1"
uv run --locked pytest tests/test_queue_controls_browser.py -m browser --override-ini="addopts=" --timeout=30 -q
```

### O4. Admin HTTP helper, даты и доступность форм

**Findings:** U01/U02; дополняет D5. **Зависимость:** D6 для долгих mutations, D5 editor state. **Файлы:** новый `app/static/js/admin-api.js`; `app/static/js/controls.js`, `app/templates/admin_daily.html`, извлечённые Daily scripts, `app/static/css/controls.css`; создать `tests/test_admin_api_js.py`, `tests/test_admin_daily_accessibility_browser.py`; расширить `tests/test_controls_ui.py`.

**JS API:** `adminRequest(url, {method='GET', body, signal, timeoutMs=10000, operationId})`; typed error `{status, code, outcomeUnknown}`. Default 10 s для GET/короткой mutation/job admission; никакого retry POST внутри helper. CSP/CSRF headers сохраняются.

- [ ] Stalled fetch по deadline освобождает busy и показывает ошибку. Abort navigation не изображает server failure. GET sequence/version guard не даёт ответу старой даты перерисовать новую.
- [ ] 401 → expired session; 403 → denied; 409 → конфликт без потери draft; 429/503 → temporary unavailable/Retry-After. POST timeout → «результат пока неизвестен» и status/revision reconciliation, без автоматической отправки duplicate.
- [ ] Долгие generate/send используют D6/D7 jobs. Для короткого CAS save после timeout прочитать revision и сравнить сохранённую секцию; draft до подтверждения не удалять.
- [ ] Server today/timezone=Europe/Kyiv в Daily bootstrap/readiness. Все default dates/семидневные фильтры используют их. Clock test: 00:30 Kyiv при предыдущем UTC date выбирает местный день. UTC image-quota и Pacific model RPD не менять.
- [ ] Grid `minmax(min(100%, 340px), 1fr)`, wrapping action rows, sticky offsets по фактической высоте. Проверить 320/375/768/1440 px и 200% zoom: нет horizontal scroll страницы; у таблицы собственная прокрутка допустима.
- [ ] Связать label/input; tabs roles/aria-selected/keyboard; dialog role/aria-modal/initial focus/trap/Escape/focus return; toasts live region; busy читаем. Dirty navigation/beforeunload действует только при настоящих drafts.
- [ ] Keyboard-only browser: вкладка → edit → cancel confirmation → save → status announcement. Background refresh не теряет focus/field value.

```powershell
uv run --locked pytest tests/test_admin_api_js.py tests/test_daily_admin_errors_js.py --override-ini="addopts=" --timeout=30 -q
$env:GEMAIBOT_REQUIRE_BROWSER_TESTS = "1"
uv run --locked pytest tests/test_admin_daily_accessibility_browser.py tests/test_controls_ui.py -m browser --override-ini="addopts=" --timeout=30 -q
```

### O5. Проверяемое качество intent и thinking

**Finding:** I01. **Файлы:** новые `tests/fixtures/intent_routing_cases.json`, `tests/test_intent_routing_corpus.py`, `scripts/evaluate_intent_routing.py`; `app/intent_router.py`, `app/thinking_classifier.py`, `app/voice_intent.py` только для диагностического результата/исправления описания; действующие intent/thinking tests, `docs/ARCHITECTURE.md`.

**Corpus schema:** case_id, language, input, history, explicit_thinking, expected_route, allowed_fallback, expected_thinking, forbidden_side_effect. Только придуманные данные. Reason codes фиксированы, не содержат input. CLI не отправляет реальные API-запросы; clients заменены fixtures.

- [ ] Минимум 60 случаев: короткие RU/UK direct intents, отрицания/цитаты, несколько задач, продолжение истории, неоднозначный город/валюта, explicit off/high, short/complex thinking, reply/forward/direct voice источники. В каждой группе минимум 5 положительных и/или отрицательных случаев; размер сам по себе не означает качество.
- [ ] У каждого случая route/fallback и запрещённое действие. Safe LLM fallback не равен ошибочной прямой отправке. Report отдельно показывает false shortcuts, missed shortcuts, explicit-setting violations и локальную latency routing без сети.
- [ ] Описание thinking classifier привести к фактическим эвристикам. Добавить reason codes без нового model call; weights по умолчанию сохранить. Не объединять corpus с необоснованной полной заменой classifier.
- [ ] CLI `uv run --locked python scripts/evaluate_intent_routing.py --output <temporary-json>` выдаёт агрегаты и failed case IDs. Tests: 100% сохранения явного thinking, 0 запрещённых side effects в corpus; остальные regressions разбираются по случаю, не маскируются общей accuracy.
- [ ] Если найден rule defect — отдельный regression/fix. По синтетическому corpus нельзя заявлять процент понимания реальных пользователей; для него нужна отдельно согласованная обезличенная выборка.

```powershell
uv run --locked pytest tests/test_intent_routing_corpus.py tests/test_thinking_classifier.py tests/test_voice_intent.py tests/test_horoscope_intent.py tests/test_direct_intent_admission_ownership.py --override-ini="addopts=" --timeout=30 -q
```

**Выход плана панелей:** оператор отличает backlog, ожидание ресурса, задержку модели и доставку; видит scope/freshness каждой цифры; выполняет только поддержанные lifecycle-команды. Скорость и понимание оцениваются измерениями, а не зелёным индикатором.
