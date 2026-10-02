# Реализация управления процессами и dashboard

Цель: единое управление моделями, fallback, предустановленными инструкциями
и поддерживаемыми параметрами выполнения без потери текущих возможностей.
Реализация разрешена пользователем после исследования 2026-09-29.
Основа: [исследование](../../model-routing-and-dashboard-review-2026-09-29.md).
Работать в текущем checkout, сохраняя предыдущие изменения; без commit/push/deploy.
Python 3.14, uv 0.12.6, Quart/Jinja; зависимости без необходимости не менять.

## 1. Завершить проверку реальных callers

- [x] Проверить router, key resolver, research, игры, media и voice callers.
- [x] Проверить runtime TTS chain, image dispatch и вспомогательные AI-процессы.
- [x] Проверить PromptRegistry и обнаружить инструкции вне него.
- [x] Составить versioned process registry с evidence paths и baseline,
  без вывода процесса только из имени поля Settings.
- [x] Для каждой политики зафиксировать executor, input capabilities,
  scope, источник, наследование, cache и apply timing.

## 2. Исправить подтверждённые препятствия

Файлы: `app/queue.py`, `app/adapters/concurrency.py`, соответствующие tests.

- [x] Регрессия Redis enqueue failure → memory task → dequeue при существующем
  Redis client; проверить отдельно пустой Redis и exception чтения.
- [x] Исправить преждевременный return, не меняя Redis priority/ack контракты.
- [x] Проверить mixed backlog statistics и достоверность backend status.
- [x] Воспроизвести global admission timeout и cancellation during acquire;
  исправить ownership release и не считать capacity rejection Redis outage.
- [x] Определить безопасные recovery/cancel semantics до добавления UI controls.

Для каждого исправления сначала failing test, затем изменение и targeted suite.

## 3. Общая политика и persistence

`app/process_policies.py` и модули `app/runtime_settings/`: registry, immutable policy values,
resolver и persistence. Web interface в отдельном Quart blueprint.

- [x] Resolver: baseline + explicit overrides + входной пользовательский выбор;
  отсутствие override, пустой fallback и отключение различаются.
- [x] Явный provider/model и capabilities; неизвестный model ID не запрещается
  только из-за статического каталога; несовместимые payload блокируются.
- [x] Атомарная versioned запись, expected revision, conflict, history/reset,
  last-known-good и degraded status при storage failure.
- [x] Snapshot на начало запроса; действующий запрос не меняется от правки UI.
- [x] Preview вызывает тот же resolver без provider calls. Проверки budgets
  охватывают вложенные key/model retries и не обходят quota reservation.

## 4. Подключение исполнителей

- [x] Chat/search/inline/document/photo: убрать скрытые расширения при явном плане,
  сохранить прежний baseline, typed events, winner и cancellation cleanup.
- [x] Research: не сбрасывать общий budget на fallback.
- [x] Games/natal/tarot/memory/summary/briefs/roles: адаптировать отдельные callers,
  сохраняя parsing/repair, consent, provenance и прямые SDK boundaries.
- [x] Image/ASR/TTS/music/Live: capabilities, преобразования payload, голос,
  chunking и session lifetime; не объявлять dispatch готовым fallback.
- [x] Отдельная процедура embeddings migration вместо опасной live-подмены.
- [x] Старые admin controls и /models согласовать с одним writer.

## 5. Предустановленные инструкции и параметры

До реализации `app/prompt_registry.py` имел 10 defaults и инвалидировал compose cache при
register; это ещё не было durable admin configuration. Исходные inline instructions были в
media, voice, games, natal, horoscope, research и image prompt preparation.

- [x] Пользователь подтвердил: «ароматы» означает «промпты».
- [x] Добавить обзор baseline/effective/source и редактирование
  шаблонов с required placeholders, diff, preview, history и reset.
- [x] Отделить editable task instructions от обязательного output schema и
  privacy/transport contracts. Проверять template variables до сохранения.
- [x] Не заменять личные пользовательские роли глобальным редактированием.
- [x] Изменения текста/маршрутов применять на новых запросах без рестарта,
  инвалидировать соответствующие caches. TTL/replica lag показывать явно.
- [x] Согласовать daily defaults и quota, сохранить доступ к preparation controls через Daily Admin; секреты
  не выдавать в dashboard. Session/startup-owned ресурсы обозначить отдельно.

### Каталог, доступность и лимиты моделей

Пользователь разрешил реорганизовать добавление, вывод, отслеживание моделей
и лимитов, сохранив работоспособность. Проверять всю цепочку, а не только форму.

- [x] Отделить identity provider/model, пользовательский каталог, internal roles,
  capability validation, account access и текущую health/availability.
- [x] Проверить `models_repo` mutations на конкурирующие записи, стабильный
  порядок, явный empty, reset и отражение на других экземплярах приложения.
- [x] Проверить источник лимита от env/`model_configuration` до actual reader,
  cache invalidation и atomic request reservation. `seed.py` сейчас выполняет
  UPSERT env DAILY_LIMITS; admin override не должен теряться при старте.
- [x] Различать provider quota и локально установленный safety cap, единицы,
  ключ/проект/модель и период сброса; не обещать общий hard cap там, где учёт
  реализован только для одного provider или request type.
- [x] Сохранить reservation перед запросом и расход на неуспешные обращения;
  retries/races должны быть видны как отдельные provider attempts.
- [x] Показать validation unavailable отдельно от unsupported и no credentials;
  новую совместимую модель можно добавить без изменения статического enum.
- [x] Тесты изменения лимита проверяют следующий admission/reservation,
  конкурентные requests, снижение ниже уже потраченного, reset и restart.

## 6. Dashboard и верификация

- [x] Новая навигация: обзор, маршруты, инструкции, нагрузка, провайдеры,
  ежедневные процессы, ошибки/история. Сохранить существующие admin actions.
- [x] Формы с черновиками, клавиатурным порядком, validation и revision conflicts;
  polling/SSE не сбрасывают focus/ввод. Empty, zero, stale, unavailable различаются.
- [x] Admin auth, browser mutation validation, bounded payload и escaped content.
- [x] Проверить UI desktop/mobile, сохранение/reset/history и реальные consumers
  после reload; не считать показанные поля доказательством подключения.
- [x] Locked Ruff, format, Mypy, offline unit/E2E; DB integration только с явно
  изолированным TEST_DATABASE_URL. Browser и live verification разделять.
- [x] Финальный аудит всех process IDs: UI → persistence → snapshot → executor,
  preserved defaults/features, prompt application, cancellation, privacy и quotas.

Завершение этапов отмечается по свежим проверкам. Этот план не является
свидетельством уже реализованных функций.

Пользователь дополнительно разрешил реорганизацию жизненного цикла ресурсов,
чтобы убрать лишние перезапуски. Для ресурсов с безопасным обновлением
предусмотреть публикацию новой конфигурации и drain старой; не путать это
с немедленным закрытием активных клиентов или заменой семафора с потерей
учёта занятых слотов. Для embeddings сохраняется отдельная migration procedure.

Первый пакет исправлений: 37 focused tests прошли (queue + local/distributed
semaphore с mock Redis). Новые тесты сначала воспроизвели 5 падений: два
варианта dequeue, mixed backlog, global timeout и cancellation during acquire.
Настоящие Redis leases/recovery и новый dashboard этим результатом не проверены.

## Финальная проверка реализации — 2026-10-02

Исходный checklist выше закрыт по проверкам рабочего дерева. Все 52 process ID
описаны в `app/runtime_settings/process_evidence.py` и представлены в `/controls`.
Каталог содержит 90 промптов: 83 действующих доступны для сравнения и редактирования,
7 исторических показаны readonly со ссылкой на текущую инструкцию;
отдельные правила вывода/consent/transport сохраняются контрактами кода.
Изменения находятся в текущем checkout; commit/push/PR/deploy не выполнялись.

| Требование / найденный риск | Реализация и evidence |
| --- | --- |
| Process registry и фактический consumer | `process_policies.py`, `runtime_settings/process_evidence.py`; 54 AST checks в `test_process_evidence.py` + runtime caller tests |
| Versioned CAS, history/reset/restore, coherent snapshot, storage failure | `runtime_settings/store.py`, lifecycle/prompts/models; `test_runtime_settings_store.py`, `test_prompt_controls.py`, `test_web_controls.py` |
| Catalog env/admin/legacy precedence, empty и restart reader | `repos/models_repo.py`, `runtime_settings/models.py`; `test_models_repo.py`, `test_runtime_model_controls.py`, `test_runtime_catalog_publication.py`; bounded legacy read не публикует частичную ревизию |
| Явная последовательность и сохранённый baseline | Typed router/request adapters и specialized callbacks; `test_process_policies.py`, `test_specialized_process_policies.py`, `test_runtime_specialized_callers.py` |
| Общий research budget и редактируемый forced synthesis | `core/research_budget.py`, `core/agentic.py`; `test_research_budget_integration.py`, `test_runtime_coverage_gaps.py` |
| Роли, memory, summary, briefs, natal/tarot и ежедневные игры | `test_runtime_operation_boundaries.py`, `test_memory_runtime_processes.py`, `test_runtime_specialized_callers.py`, focused privacy/game suites; retry роли использует ту же политику |
| Полный media/provider охват | `test_provider_result_processes.py`, `test_media_process_policies.py`, `test_media_chain_deadlines.py`, `test_voice_race_cleanup.py`; Gemini/FTA/Pollinations images, music, ASR, ElevenLabs/Gemini TTS, Live/Vertex |
| Срок полного сообщения и переход к следующему провайдеру TTS | Общий срок configured Gemini model plan, отмена/await дочерних SDK задач, следующий configured delivery provider после deadline; `test_voice_race_cleanup.py` |
| Квота до SDK, включая baseline и retries/races | `test_baseline_gemini_quota.py` (38 cases), `test_image_quota_reservation.py`, media/TTS tests; локальный отказ не штрафует provider health |
| Модели вне публичного каталога и truthful limit metrics | `runtime_settings/models.py`, `repos/metrics_repo.py`; `test_runtime_usage_metrics.py`, `test_runtime_coverage_gaps.py`; null override и integer admission threshold отражены в usage |
| Совместимость новых ID | Новые синтаксически совместимые ID не зависят от enum; известные FTA media ID отклоняются для текста; unsupported и validation unavailable различаются |
| Cache invalidation | `runtime_settings/cache_identity.py`; `test_runtime_cache_identity.py`, `test_prompt_controls.py`, vision intent tests; вставленные данные не интерпретируются повторно |
| Согласованные writers старых форм | Crocodile, inline, Trivia и `/models` используют versioned CAS; `test_runtime_process_writers.py`, `test_croc_admin_controls.py`, `test_daily_text_model.py` |
| Свежий snapshot долгоживущих workers | Detached context сохраняет tracing/privacy, снимая pin первого enqueue; `test_background_runtime_snapshots.py` проверяет два реальных VoiceJob и обе task APIs |
| Redis ownership/recovery и mixed statistics | Уникальный owner, lease 60s/renew 15s, fenced claim/ACK/NACK/recovery; `test_redis_queue.py`, `test_distributed_semaphore.py`, `test_dashboard_snapshot.py` |
| Безопасный legacy queue cutover и embedding transition | Явные эксплуатационные процедуры в [runtime controls](../../runtime-controls.md); старый processing list не забирается автоматически, embeddings остаются readonly |
| UI и HTTP boundary | `test_web_controls.py`, `test_controls_ui.py`; CSRF/auth, bounded streamed body, 409 без потери черновика, escaped output; сохранение новой правки во время POST, явный committed/refresh-failed статус и игнорирование устаревшего GET |
| Desktop/mobile и применение без reload | Настоящие формы/validators/CAS с подменённой DB: 52/90 карточек, пять save/reset, conflict/rebase, internal model в limit list; отдельные разделы, мобильный sticky selector/refresh, URL filters/Back, поиск черновика, клавиатура/discard/restore confirm; ноль перезагрузок документа |
| Достоверность мониторинга | LIVE → STALE при ошибке fetch с отдельным цветом, неизвестный backlog вместо нуля, отдельные local/Redis значения и scope текущего процесса; все четыре mobile tabs видны, keyboard/hash/Back проверены |
| Качество промптов и фактические потребители | [Аудит всех 90 ID](../../prompt-quality-audit-2026-10-02.md); исправлены photo cardinality, JSON-примеры, Trivia answer/index, summary replacement, research evidence/language, tags, TTS/ASR и domain инструкции; 8 новых regression tests и 255 focused tests |
| Встроенные/исторические инструкции | 7 дополнительных presets в `additional_prompts.py`, реальные consumer tests для horoscope/Croc/Whisper/inline/continue; `prompt_usage.py` запрещает неэффективные новые legacy writes, сохраняя старую историю |

Полный offline unit/E2E прогон текущего дерева: **3556 passed, 28 skipped**,
83,35 секунды. Команда:

```powershell
uv run --locked pytest tests/ --ignore=tests/integration -m "not integration" --override-ini="addopts=" -n 4 --dist=loadgroup --timeout=30 --tb=short -q
```

Locked Ruff check и format check проходят (648 files); Mypy не нашёл ошибок
в 275 source-файлах. Отчёты: `output/runtime-controls-tests.log` и
`output/runtime-controls-tests.xml`. Browser evidence: `output/playwright/`
с `controls-final-*` и `dashboard-final-*` screenshots. Пропуски не считаются
успешными проверками.

Сохранены мониторинг, provider/key health, история ошибок и Daily Admin actions.
Новый `/controls` дополняет их общим редактором. Настройки маршрутов/текста/лимитов
не требуют рестарта; новые Live сеансы читают новую конфигурацию, активные
сохраняют прежний transport. Startup-owned env/credentials/pools/worker count
остаются в действующем lifecycle. TTL кэша 5 секунд проверяется при следующем
обращении, а не гарантирует доставку обновления за 5 секунд.

Реальные Telegram/provider/Redis/PostgreSQL/VPS, production queue cutover и
embedding reindex этими локальными проверками не подтверждены. SQL migrations
и destructive integration checks без изолированных сервисов не выполнялись.
Redis recovery имеет семантику at-least-once; это не гарантия exactly-once или
универсальной replica safety. Общий RPD относится к Gemini text/ASR/TTS;
image, embeddings и Live имеют отдельно обозначенные границы.
