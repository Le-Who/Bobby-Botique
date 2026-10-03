# Исправления корпуса тестов — 2026-10-03

Закрыты все **43 записи** исходного [аудита](test-corpus-audit-2026-10-03.md): 2 P1, 37 P2 и 4 P3. HAR-008 и PROD-006 описывали один DB-harness с разных сторон и исправлены совместно.

Проверенный код: `b4a22b60e73d5f9f2263da5ea64abc2cedd97a7d`. Все восемь jobs [CI](https://github.com/Le-Who/Bobby-Botique/actions/runs/37089412317) и [автоматический deploy](https://github.com/Le-Who/Bobby-Botique/actions/runs/37089539967) завершились успешно. Это датированное доказательство для указанного checkout, а не гарантия всех свойств живого бота.

Оригинальные inventory и JSON описывают `425563b4` и сохранены. Все 352 source hashes сверены с этим Git-архивом с учётом LF/CRLF checkout; содержимое старых тестов не заменялось новой версией. Полная привязка изменений и проверок — в [JSON-приложении](test-corpus-fixes-evidence-2026-10-03.json).

## Изменения по каждой находке

| ID | Исправленный контракт | Основной файл |
| --- | --- | --- |
| AI-MEM-001 | Policy timeout отделён от SDK timeout; проверены SDK entry, cancellation cleanup, fallback и квота. | [test_memory_query_expansion_timeout.py](../tests/test_memory_query_expansion_timeout.py) |
| AI-MEM-002 | Voice privacy требует реального lease enter/exit и ASR-вызова либо его запрета. | [test_msg_voice_privacy.py](../tests/test_msg_voice_privacy.py) |
| AI-MEM-003 | First-text deadline общий для попыток; тест отклоняет его перезапуск. | [test_provider_model_hedge.py](../tests/test_provider_model_hedge.py) |
| AI-MEM-004 | Multimodal routing проверяет фактически выбранного провайдера и payload. | [test_provider_router_integration.py](../tests/test_provider_router_integration.py) |
| AI-MEM-005 | Graph fake возвращает актуальные три значения; проверены graph branch и provenance. | [test_ai_chat.py](../tests/test_ai_chat.py) |
| AI-MEM-006 | Optimization cases вызывают настоящее research search tool и проверяют сортировку/дедупликацию. | [test_agent_optimization.py](../tests/test_agent_optimization.py) |
| AI-MEM-007 | Key-health persistence отделён от офлайн router tests; неявного DB I/O нет. | [test_provider_router_integration.py](../tests/test_provider_router_integration.py) |
| AI-MEM-008 | Oversized summary проверяет используемый execute_text_process seam и приватность входа. | [test_context_summarizer.py](../tests/test_context_summarizer.py) |
| AI-MEM-009 | Параллельные tools должны одновременно достичь Event-барьера. | [test_agentic_improvements.py](../tests/test_agentic_improvements.py) |
| AI-MEM-010 | Media reserve использует оставшийся общий deadline и собственную квоту. | [test_media_chain_deadlines.py](../tests/test_media_chain_deadlines.py) |
| AI-MEM-011 | Live WebSocket auth требует close 4003 до доступа к provider/session. | [test_live_audio.py](../tests/test_live_audio.py) |
| AI-MEM-012 | ASR input проверяет реальные PCM bytes и MIME; scripted output не подменяет эту проверку. | [test_live_audio.py](../tests/test_live_audio.py) |
| HAR-001 | Офлайн E2E выполняются без DB; исправлены async quota fake и конфигурационный seam vision fallback. | [conftest.py](../tests/e2e/conftest.py) |
| HAR-002 | Шесть DB-модулей вызывают API репозиториев; метрики используют настоящий writer/reader; schema smoke обозначен отдельно. | [test_repos_chats.py](../tests/integration/test_repos_chats.py) |
| HAR-003 | Lifecycle assertions получают реальные workload events при success/error/cancellation. | [test_incident_scenarios.py](../tests/observability/test_incident_scenarios.py) |
| HAR-004 | HTTP-запросы идут в маршрут, зарегистрированный bot lifecycle; проверены malformed/auth/duplicate/overload и конкурентный retry. | [test_webhook_lifecycle.py](../tests/e2e/test_webhook_lifecycle.py) |
| HAR-005 | Специализированные memory SDK вызовы изолированы, реальная квота проверена; request tasks принадлежат тесту и ожидаются. | [test_chat_happy_path.py](../tests/e2e/test_chat_happy_path.py) |
| HAR-006 | Общий transactional helper: без compatibility DDL, явные rollback/close, видимые setup/cleanup ошибки и отмена. | [database_fixtures.py](../tests/database_fixtures.py) |
| HAR-007 | Проверяется настоящий message handler и callback: busy rejection, освобождение lock и повторный приём после завершения/отмены. | [test_request_concurrency.py](../tests/test_request_concurrency.py) |
| HAR-008 | Неиспользуемый optional-container harness удалён; подписки выполняются через общий изолированный PostgreSQL в CI. | [conftest.py](../tests/conftest.py) |
| PLAT-001 | Scoped patch реальных модулей заменяет reload/sys.modules substitution; identity восстанавливается. | [test_system_status.py](../tests/test_system_status.py) |
| PLAT-002 | Database aliases и group singleton сохраняют согласованную identity после теста. | [test_audit_fixes.py](../tests/test_audit_fixes.py) |
| PLAT-003 | Waiting-message smoke детерминирован и не обращается к DB. | [test_waiting_facts.py](../tests/test_waiting_facts.py) |
| PLAT-004 | Проверяется реальная очистка заполненного cache вместо принудительного no-op. | [test_cache_ttl.py](../tests/test_cache_ttl.py) |
| PLAT-005 | Image executor выполняет настоящий worker; проверены результат и отказ. | [test_io_handlers.py](../tests/test_io_handlers.py) |
| PLAT-006 | Oracles отклоняют italicized __init__ и необработанные угловые скобки; formatter исправлен. | [test_formatting_e2e.py](../tests/test_formatting_e2e.py) |
| PLAT-007 | AST-проверка требует настоящий awaited Local Bot API release перед последующим этапом. | [test_local_bot_api_release_contract.py](../tests/test_local_bot_api_release_contract.py) |
| PLAT-008 | Восемь Redis cases исполняют production Lua constants: fencing, ownership, retry и recovery. | [test_queue_lua.py](../tests/integration/test_queue_lua.py) |
| PLAT-009 | Reminder tasks создаются, отменяются и ожидаются; незапланированных coroutine в registry нет. | [test_cmd_reminders.py](../tests/test_cmd_reminders.py) |
| PLAT-010 | Проверены фактический вход/выход lease и пиковая взаимная исключительность semaphore. | [test_semaphore_invariants.py](../tests/test_semaphore_invariants.py) |
| PLAT-011 | Cached limit fake соблюдает scalar integer contract и проверяет реальный writer. | [test_repos_keys.py](../tests/test_repos_keys.py) |
| PLAT-012 | Decryption failure проходит через настоящую router boundary к пользовательскому сообщению. | [test_decryption_error_handling.py](../tests/test_decryption_error_handling.py) |
| PLAT-013 | Functional Event/barrier assertions заменяют короткие wall-time пределы. | [test_callback_responsiveness_scenario.py](../tests/test_callback_responsiveness_scenario.py) |
| PLAT-014 | Resource-owning cases закрывают ресурсы и ожидают отменённые задачи также на failure path. | [test_taskmanager_bounded.py](../tests/test_taskmanager_bounded.py) |
| PLAT-015 | Невалидный auth header действительно отправляется в HTTP-запросе. | [test_web_security.py](../tests/test_web_security.py) |
| PROD-001 | Настоящий game WebSocket endpoint проверяет точные close codes и запрет side effects. | [test_game_websocket.py](../tests/test_game_websocket.py) |
| PROD-002 | Restart cache test использует реальные UTF-8 file writer/reader, topic isolation и повреждённый файл. | [test_game_cache.py](../tests/test_game_cache.py) |
| PROD-003 | Изображения проверяют используемую Pollinations factory, дату, enhance=False, Easy/Hard и Telegram upload. | [test_daily_crocodile_optimizations.py](../tests/test_daily_crocodile_optimizations.py) |
| PROD-004 | Отрицательные model cases следят за фактическими catalog/settings writers. | [test_daily_trivia.py](../tests/test_daily_trivia.py) |
| PROD-005 | Debounce проверяет Event и identity task; cleanup выполняется в finally. | [test_daily_crocodile.py](../tests/test_daily_crocodile.py) |
| PROD-006 | Две проверки подписок маркированы integration и выполняются в обязательном PostgreSQL job. | [test_horoscope_subscriptions.py](../tests/test_horoscope_subscriptions.py) |
| PROD-007 | Telegraph guard требует assert_not_awaited после возврата реального оркестратора. | [test_natal_service.py](../tests/test_natal_service.py) |
| PROD-008 | LLM repair assertions вынесены из callback, где fallback мог поглотить их ошибку. | [test_natal_llm.py](../tests/test_natal_llm.py) |

## Ошибки приложения, обнаруженные регрессиями

- После webhook `503` повторный update сохраняет возможность попасть в очередь: ещё не принятый ID не отмечается обработанным. Блокировка приёма охватывает Redis await, поэтому два запроса не могут занять последний слот и потерять retry. Проверены обычное сообщение, команда и конкурентный Redis claim.
- [UserStateRow](../app/core/entities.py) сохраняет `role_diaries` при чтении, включая независимый пустой default. [Conversation API](../app/repos/conversations.py) возвращает `False` для отсутствующего/чужого rename и стабильно сортирует одинаковые timestamps по `id`.
- [Миграция 073](../scripts/migrations/073_save_model_conversation_turns.sql) сохраняет `model` и legacy `assistant`, исходный порядок и фильтры служебных сообщений. Процедура проверяет принадлежность target conversation пользователю даже при privileged DB-role. Прежняя миграция 005, signature, invoker rights и search path сохранены.
- [Metrics writer](../app/metrics.py) складывает JSONB-счётчики по ключам внутри атомарного UPSERT; повторная выгрузка сохраняет накопленное значение. Проверены глобальные и пользовательские счётчики, новые и сохранённые ключи, пустые изменения, SQL NULL и старые значения array/string.
- [Formatter](../app/utils/text_format.py) сохраняет поддерживаемые Python dunder names, включая `__init__`, сохраняя проверенный legacy Markdown contract.
- [Research boundary](../app/core/agentic.py) сохраняет причину общего deadline при истечении внутреннего таймаута синтеза. Ранний `TimeoutError` самого SDK оставляет неизрасходованный бюджет. Детерминированные тесты проверяют оба порядка таймаутов, завершение отменённого SDK-вызова, известные и неизвестные токены, ошибки провайдера и внешнюю отмену.

До SQL-исправления [изолированный CI](https://github.com/Le-Who/Bobby-Botique/actions/runs/37088548894) воспроизвёл потерю `model`, запись процедурой в чужой диалог и обе перезаписи JSONB-счётчиков; 81 сценарий прошёл, 6 упали. Одно из шести падений было неверным sync context manager для asyncpg savepoint в новом schema smoke; это исправлено через `async with`. Отдельный [Linux контрпример](https://github.com/Le-Who/Bobby-Botique/actions/runs/37088390568) выявил потерю `budget_reason` при research timeout.

## Выполненная проверка

| Набор | Результат | Время |
| --- | --- | --- |
| Локальный unit/E2E, четыре workers, Chromium включён | 3783 passed; 0 skipped/errors/failures | 91,61 с |
| Linux CI unit/E2E | 3778 passed, 5 skipped; эти пять случаев выполняются в browser job | 41,92 с |
| CI PostgreSQL/pgvector и Redis | 94 passed; 0 skipped/errors/failures | 19,68 с |
| Обязательный Chromium job | 5 passed; 0 skipped/errors/failures | 8,49 с |

Всего собираются **3877 случаев** в **357 tracked Python-файлах** под `tests/`.
Все 3877 случаев выполнены в совокупности CI jobs; пять минимальных unit-пропусков
закрыты отдельным browser job. Миграция `073` применена успешно; CI повторно
применил всю цепочку и проверил отсутствие pending migrations.

- Профильные offline metrics/migration/repository checks: 76 passed; schema/RLS catalog invariants и уникальность UTF-8 manifest включены.
- Общие DB-fixture tests: 12 случаев, включая setup failure, rollback failure и cancellation. В CI реальные transactional fixtures проходят против мигрированной базы.
- CI отдельно выполняет unit/E2E, изолированные PostgreSQL/pgvector и Redis, пять обязательных Chromium cases, locked Ruff/Mypy, production dependency audit/SBOM, image smoke и observability configuration/readiness.
- AI/memory: 19 контролируемых mutations обнаружены усиленными тестами. Platform: 16 регрессионных контрпримеров отклонены; семь module identity/restoration predicates прошли. Product: 16 ожидаемых assertion failures после контролируемых faults; старые 12 проверок пропускали соответствующие ошибки. Эти числа относятся к разным методам и не складываются в один mutation score.

Команды для повторения и prerequisites обновлены в [CONTRIBUTING](../CONTRIBUTING.md). RuntimeWarning и PytestUnraisableExceptionWarning теперь являются ошибками теста; глобальное подавление asyncpg loop errors удалено.

## Основания и границы

Fixtures владеют ресурсом и чистят его также при setup error/failure; это соответствует [pytest fixture guidance](https://docs.pytest.org/en/stable/how-to/fixtures.html). Scoped patch восстанавливает bindings по правилам [monkeypatch](https://docs.pytest.org/en/stable/how-to/monkeypatch.html). Event/barrier проверки отделяют correctness от scheduling margins, как рекомендует [pytest flaky-test guidance](https://docs.pytest.org/en/stable/explanation/flaky.html).

Browser dependencies закреплены в отдельном npm lock; CI устанавливает их и Chromium по [Playwright CI guidance](https://playwright.dev/docs/ci-intro). PostgreSQL `jsonb ||` заменяет дублирующиеся ключи, поэтому он не подходит для сложения delta counters — [PostgreSQL 17 JSON functions](https://www.postgresql.org/docs/17/functions-json.html). Корректирующая процедура сохраняет invoker/security semantics по [CREATE PROCEDURE](https://www.postgresql.org/docs/17/sql-createprocedure.html).

Исправления процедуры и счётчиков обеспечивают правильность последующих
сохранений и выгрузок. Уже утраченные реплики или значения исторических
счётчиков требуют исходных данных для восстановления.

Локально PostgreSQL/Redis не запускались: их execution evidence получено в изолированном CI. Эти проверки не устанавливают универсальную tenant isolation для privileged roles, live Telegram/provider availability или полный путь ingestion production logs. Audit baseline, supplied revisions и временные диагностические артефакты сохранены.
