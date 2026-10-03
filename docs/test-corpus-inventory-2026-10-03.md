# Реестр проверки тестов — 2026-10-03

Checkout: `425563b4178f1ff63c3589e772e13c4d6485c630`. Все 352 tracked Python-файла прочитаны полностью; непроверенных файлов нет. Это реестр source review, а не заявление, что каждый сценарий выполнен или доказан мутацией.

Основной [отчёт](test-corpus-audit-2026-10-03.md) содержит критерии, результаты, границы и приоритеты. [JSON-доказательства](test-corpus-audit-evidence-2026-10-03.json) включают пояснения по каждому файлу, source hash и полные evidence/recommendation для находок.

В колонке «Локальный unit» порядок чисел: **passed / skipped / failed**. Знак «—» означает, что файл не выполнялся в этом выборе либо является вспомогательным. CI integration указывает число реально выбранных случаев; все 65 прошли. Отдельный probe DB-free E2E дал 23 passed / 3 failed и не заменяет первоначальные skipped значения в таблице.

«Прочитан» означает проверку тел, фикстур и актуального контракта. «—» в находках означает, что подтверждённого замечания в рамках данного аудита нет; это не абсолютная гарантия корректности всех assertions.

## Фикстуры, E2E, SQL, observability и CI

Файлов: **49**; собранных случаев: **217**; все прочитаны.

| Файл | Прочитан | Собрано | Локальный unit | CI integration | Находки |
| --- | --- | ---: | --- | ---: | --- |
| [tests/conftest.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/conftest.py) | Да | 0 | — | — | — |
| [tests/database_safety.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/database_safety.py) | Да | 0 | — | — | — |
| [tests/e2e/__init__.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/__init__.py) | Да | 0 | — | — | — |
| [tests/e2e/conftest.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/conftest.py) | Да | 0 | — | — | HAR-001, HAR-006 |
| [tests/e2e/test_chat_happy_path.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/test_chat_happy_path.py) | Да | 1 | — | 1 | HAR-005 |
| [tests/e2e/test_crocodile_engine.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/test_crocodile_engine.py) | Да | 16 | 0 / 16 / 0 | — | HAR-001 |
| [tests/e2e/test_opencode_cascade.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/test_opencode_cascade.py) | Да | 6 | 0 / 6 / 0 | — | HAR-001 |
| [tests/e2e/test_webhook_lifecycle.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/test_webhook_lifecycle.py) | Да | 4 | 0 / 4 / 0 | — | HAR-001, HAR-004 |
| [tests/factories.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/factories.py) | Да | 0 | — | — | — |
| [tests/fixtures/db_container.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/fixtures/db_container.py) | Да | 0 | — | — | HAR-008 |
| [tests/integration/__init__.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/__init__.py) | Да | 0 | — | — | — |
| [tests/integration/conftest.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/conftest.py) | Да | 0 | — | — | HAR-006 |
| [tests/integration/test_concurrency_locks.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_concurrency_locks.py) | Да | 2 | — | 2 | HAR-007 |
| [tests/integration/test_db_repos.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_db_repos.py) | Да | 14 | — | 14 | HAR-002 |
| [tests/integration/test_e2e_app_smoke.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_e2e_app_smoke.py) | Да | 1 | — | 1 | — |
| [tests/integration/test_memory_repo.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_memory_repo.py) | Да | 2 | — | 2 | — |
| [tests/integration/test_metrics_jsonb.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_metrics_jsonb.py) | Да | 5 | — | 5 | HAR-002 |
| [tests/integration/test_pgvector_integration.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_pgvector_integration.py) | Да | 1 | — | 1 | — |
| [tests/integration/test_repos_chats.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_repos_chats.py) | Да | 6 | — | 6 | HAR-002 |
| [tests/integration/test_repos_conversations.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_repos_conversations.py) | Да | 6 | — | 6 | HAR-002 |
| [tests/integration/test_repos_keys.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_repos_keys.py) | Да | 7 | — | 7 | HAR-002 |
| [tests/integration/test_repos_roles.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_repos_roles.py) | Да | 6 | — | 6 | HAR-002 |
| [tests/integration/test_repos_user_stats.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_repos_user_stats.py) | Да | 5 | — | 5 | HAR-002 |
| [tests/integration/test_repos_users.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_repos_users.py) | Да | 4 | — | 4 | HAR-002 |
| [tests/integration/test_web_security.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_web_security.py) | Да | 4 | — | 4 | — |
| [tests/observability/__init__.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/__init__.py) | Да | 0 | — | — | — |
| [tests/observability/test_bootstrap.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_bootstrap.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/observability/test_collector_contract.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_collector_contract.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/observability/test_config.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_config.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/observability/test_content.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_content.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/observability/test_context_lifecycle.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_context_lifecycle.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/observability/test_database_events.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_database_events.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/observability/test_delivery_events.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_delivery_events.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/observability/test_deployment_contract.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_deployment_contract.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/observability/test_diagnostics.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_diagnostics.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/observability/test_events.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_events.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/observability/test_incident_export.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_incident_export.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/observability/test_incident_scenarios.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_incident_scenarios.py) | Да | 7 | 7 / 0 / 0 | — | HAR-003 |
| [tests/observability/test_ingress.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_ingress.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/observability/test_log_viewer_smoke.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_log_viewer_smoke.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/observability/test_operational_metrics.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_operational_metrics.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/observability/test_pipeline.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_pipeline.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/observability/test_provider_events.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_provider_events.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/observability/test_redaction.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_redaction.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/observability/test_workload_events.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_workload_events.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/observability/test_writer.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_writer.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_ci_workflow_config.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_ci_workflow_config.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_repository_safety_contract.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_repository_safety_contract.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_test_environment_safety.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_test_environment_safety.py) | Да | 9 | 9 / 0 / 0 | — | — |

## Игровые и natal сценарии

Файлов: **73**; собранных случаев: **953**; все прочитаны.

| Файл | Прочитан | Собрано | Локальный unit | CI integration | Находки |
| --- | --- | ---: | --- | ---: | --- |
| [tests/test_admin_daily_broadcast.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_admin_daily_broadcast.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_admin_dailycroc_csp.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_admin_dailycroc_csp.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_astro.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_astro.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/test_cmd_tarot.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cmd_tarot.py) | Да | 13 | 13 / 0 / 0 | — | — |
| [tests/test_compatibility.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_compatibility.py) | Да | 36 | 36 / 0 / 0 | — | — |
| [tests/test_compatibility_handlers.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_compatibility_handlers.py) | Да | 43 | 43 / 0 / 0 | — | — |
| [tests/test_croc_admin_controls.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_croc_admin_controls.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_croc_admin_controls_js.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_croc_admin_controls_js.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_croc_model_sources.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_croc_model_sources.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_croc_process_models.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_croc_process_models.py) | Да | 16 | 16 / 0 / 0 | — | — |
| [tests/test_crocodile_shared_judge.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_crocodile_shared_judge.py) | Да | 9 | 9 / 0 / 0 | — | — |
| [tests/test_crocodile_shared_words.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_crocodile_shared_words.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_crocodile_telegram.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_crocodile_telegram.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_daily_2048.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_2048.py) | Да | 29 | 29 / 0 / 0 | — | — |
| [tests/test_daily_croc_admin_js.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_croc_admin_js.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_daily_croc_models.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_croc_models.py) | Да | 6 | 6 / 0 / 0 | — | PROD-004 |
| [tests/test_daily_croc_quota.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_croc_quota.py) | Да | 21 | 21 / 0 / 0 | — | — |
| [tests/test_daily_crocodile.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_crocodile.py) | Да | 27 | 27 / 0 / 0 | — | PROD-005 |
| [tests/test_daily_crocodile_optimizations.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_crocodile_optimizations.py) | Да | 11 | 11 / 0 / 0 | — | PROD-003 |
| [tests/test_daily_game_choice.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_game_choice.py) | Да | 27 | 27 / 0 / 0 | — | — |
| [tests/test_daily_key_manager.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_key_manager.py) | Да | 19 | 19 / 0 / 0 | — | — |
| [tests/test_daily_preparation.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_preparation.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_daily_text_model.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_text_model.py) | Да | 33 | 33 / 0 / 0 | — | — |
| [tests/test_daily_trivia.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_trivia.py) | Да | 44 | 44 / 0 / 0 | — | PROD-004 |
| [tests/test_daily_unreachable_delivery.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_unreachable_delivery.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_daily_word_locking.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_word_locking.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_deploy_natal_gate.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_deploy_natal_gate.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_destiny_matrix.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_destiny_matrix.py) | Да | 13 | 13 / 0 / 0 | — | — |
| [tests/test_game_auth.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_game_auth.py) | Да | 12 | 12 / 0 / 0 | — | — |
| [tests/test_game_cache.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_game_cache.py) | Да | 27 | 27 / 0 / 0 | — | PROD-002 |
| [tests/test_game_hints.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_game_hints.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_game_inline.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_game_inline.py) | Да | 15 | 15 / 0 / 0 | — | — |
| [tests/test_game_judge_integration.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_game_judge_integration.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_game_llm_tasks.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_game_llm_tasks.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_game_websocket.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_game_websocket.py) | Да | 8 | 8 / 0 / 0 | — | PROD-001 |
| [tests/test_games.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_games.py) | Да | 95 | 95 / 0 / 0 | — | — |
| [tests/test_horoscope_catchup.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_horoscope_catchup.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_horoscope_intent.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_horoscope_intent.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_horoscope_on_demand.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_horoscope_on_demand.py) | Да | 15 | 15 / 0 / 0 | — | — |
| [tests/test_horoscope_scheduled.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_horoscope_scheduled.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_horoscope_subscription_entrypoints.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_horoscope_subscription_entrypoints.py) | Да | 17 | 17 / 0 / 0 | — | — |
| [tests/test_horoscope_subscription_messages.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_horoscope_subscription_messages.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_horoscope_subscriptions.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_horoscope_subscriptions.py) | Да | 4 | 2 / 2 / 0 | — | PROD-006 |
| [tests/test_natal_accuracy.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_accuracy.py) | Да | 23 | 23 / 0 / 0 | — | — |
| [tests/test_natal_astronomy.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_astronomy.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_natal_calculator.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_calculator.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_natal_city_catalog.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_city_catalog.py) | Да | 42 | 42 / 0 / 0 | — | — |
| [tests/test_natal_city_readiness.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_city_readiness.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_natal_config.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_config.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_natal_config_readiness.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_config_readiness.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_natal_geocoding.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_geocoding.py) | Да | 16 | 16 / 0 / 0 | — | — |
| [tests/test_natal_handler.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_handler.py) | Да | 41 | 41 / 0 / 0 | — | — |
| [tests/test_natal_intent.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_intent.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_natal_llm.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_llm.py) | Да | 19 | 19 / 0 / 0 | — | PROD-008 |
| [tests/test_natal_maintenance.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_maintenance.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_natal_miniapp.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_miniapp.py) | Да | 13 | 13 / 0 / 0 | — | — |
| [tests/test_natal_models.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_models.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_natal_parser.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_parser.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_natal_readiness.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_readiness.py) | Да | 18 | 18 / 0 / 0 | — | — |
| [tests/test_natal_report_builder.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_report_builder.py) | Да | 29 | 29 / 0 / 0 | — | — |
| [tests/test_natal_report_ids.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_report_ids.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_natal_service.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_service.py) | Да | 7 | 7 / 0 / 0 | — | PROD-007 |
| [tests/test_natal_smoke.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_smoke.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_natal_smoke_cli.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_smoke_cli.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_natal_storage.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_storage.py) | Да | 12 | 12 / 0 / 0 | — | — |
| [tests/test_natal_svg_renderer.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_svg_renderer.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_natal_text_safety.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_text_safety.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_natal_web_report.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_web_report.py) | Да | 8 | 3 / 5 / 0 | — | — |
| [tests/test_tarot.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_tarot.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_tarot_chat.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_tarot_chat.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_tarot_daily_handler.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_tarot_daily_handler.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_tarot_daily_precompute.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_tarot_daily_precompute.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/test_tarot_inline_retry.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_tarot_inline_retry.py) | Да | 11 | 11 / 0 / 0 | — | — |

## Провайдеры, AI, медиа и память

Файлов: **76**; собранных случаев: **942**; все прочитаны.

| Файл | Прочитан | Собрано | Локальный unit | CI integration | Находки |
| --- | --- | ---: | --- | ---: | --- |
| [tests/test_additional_prompt_consumers.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_additional_prompt_consumers.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_agent_optimization.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_agent_optimization.py) | Да | 3 | 3 / 0 / 0 | — | AI-MEM-006 |
| [tests/test_agent_use_cases.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_agent_use_cases.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_agentic_improvements.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_agentic_improvements.py) | Да | 36 | 36 / 0 / 0 | — | AI-MEM-009 |
| [tests/test_agentic_search.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_agentic_search.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_ai_budget.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_ai_budget.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_ai_chat.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_ai_chat.py) | Да | 18 | 18 / 0 / 0 | — | AI-MEM-005 |
| [tests/test_ai_core.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_ai_core.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_ai_document.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_ai_document.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_ai_photo.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_ai_photo.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_ai_provider.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_ai_provider.py) | Да | 18 | 18 / 0 / 0 | — | — |
| [tests/test_ai_search.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_ai_search.py) | Да | 9 | 9 / 0 / 0 | — | — |
| [tests/test_audio_processor.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_audio_processor.py) | Да | 14 | 14 / 0 / 0 | — | — |
| [tests/test_background_runtime_snapshots.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_background_runtime_snapshots.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_baseline_gemini_quota.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_baseline_gemini_quota.py) | Да | 38 | 38 / 0 / 0 | — | — |
| [tests/test_build_ocr_prompt.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_build_ocr_prompt.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_cb_ai_actions.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cb_ai_actions.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_context_assembler.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_context_assembler.py) | Да | 44 | 44 / 0 / 0 | — | — |
| [tests/test_context_compression.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_context_compression.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_context_summarizer.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_context_summarizer.py) | Да | 16 | 16 / 0 / 0 | — | AI-MEM-008 |
| [tests/test_elevenlabs_tts.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_elevenlabs_tts.py) | Да | 27 | 27 / 0 / 0 | — | — |
| [tests/test_gemini_provider.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_gemini_provider.py) | Да | 14 | 14 / 0 / 0 | — | — |
| [tests/test_live_audio.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_live_audio.py) | Да | 20 | 20 / 0 / 0 | — | AI-MEM-011, AI-MEM-012 |
| [tests/test_ltm_cleanup_scheduler.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_ltm_cleanup_scheduler.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_ltm_schema_graph_baseline.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_ltm_schema_graph_baseline.py) | Да | 48 | 48 / 0 / 0 | — | — |
| [tests/test_media_chain_deadlines.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_media_chain_deadlines.py) | Да | 4 | 4 / 0 / 0 | — | AI-MEM-010 |
| [tests/test_media_download.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_media_download.py) | Да | 27 | 27 / 0 / 0 | — | — |
| [tests/test_media_process_policies.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_media_process_policies.py) | Да | 12 | 12 / 0 / 0 | — | AI-MEM-007 |
| [tests/test_memory_commands.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_memory_commands.py) | Да | 9 | 9 / 0 / 0 | — | — |
| [tests/test_memory_consent.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_memory_consent.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_memory_consolidation_safety.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_memory_consolidation_safety.py) | Да | 19 | 19 / 0 / 0 | — | — |
| [tests/test_memory_extraction_key_rotation.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_memory_extraction_key_rotation.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_memory_extraction_provenance.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_memory_extraction_provenance.py) | Да | 17 | 17 / 0 / 0 | — | — |
| [tests/test_memory_graph_writer.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_memory_graph_writer.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_memory_manager.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_memory_manager.py) | Да | 12 | 12 / 0 / 0 | — | — |
| [tests/test_memory_query_expansion_timeout.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_memory_query_expansion_timeout.py) | Да | 1 | 1 / 0 / 0 | — | AI-MEM-001 |
| [tests/test_memory_runtime_processes.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_memory_runtime_processes.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_msg_voice_privacy.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_msg_voice_privacy.py) | Да | 1 | 1 / 0 / 0 | — | AI-MEM-002 |
| [tests/test_multimodal_key_hash.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_multimodal_key_hash.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_opencode_routing.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_opencode_routing.py) | Да | 38 | 38 / 0 / 0 | — | — |
| [tests/test_openrouter_provider.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_openrouter_provider.py) | Да | 12 | 12 / 0 / 0 | — | — |
| [tests/test_pollinations_provider.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_pollinations_provider.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/test_private_data_leases.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_private_data_leases.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_process_evidence.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_process_evidence.py) | Да | 54 | 54 / 0 / 0 | — | — |
| [tests/test_process_policies.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_process_policies.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/test_prompt_controls.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_prompt_controls.py) | Да | 29 | 29 / 0 / 0 | — | — |
| [tests/test_prompt_quality_contracts.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_prompt_quality_contracts.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_prompt_registry.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_prompt_registry.py) | Да | 29 | 29 / 0 / 0 | — | — |
| [tests/test_prompts.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_prompts.py) | Да | 14 | 14 / 0 / 0 | — | — |
| [tests/test_provider_model_hedge.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_provider_model_hedge.py) | Да | 6 | 6 / 0 / 0 | — | AI-MEM-003 |
| [tests/test_provider_result_processes.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_provider_result_processes.py) | Да | 13 | 13 / 0 / 0 | — | AI-MEM-010 |
| [tests/test_provider_router.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_provider_router.py) | Да | 26 | 26 / 0 / 0 | — | — |
| [tests/test_provider_router_integration.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_provider_router_integration.py) | Да | 6 | 6 / 0 / 0 | — | AI-MEM-004, AI-MEM-007 |
| [tests/test_provider_stream_types.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_provider_stream_types.py) | Да | 17 | 17 / 0 / 0 | — | — |
| [tests/test_repo_memory.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_repo_memory.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_request_context.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_request_context.py) | Да | 14 | 14 / 0 / 0 | — | — |
| [tests/test_research_budget.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_research_budget.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_research_budget_integration.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_research_budget_integration.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_runtime_cache_identity.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_runtime_cache_identity.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_runtime_catalog_publication.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_runtime_catalog_publication.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_runtime_coverage_gaps.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_runtime_coverage_gaps.py) | Да | 23 | 23 / 0 / 0 | — | — |
| [tests/test_runtime_gemini_execution.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_runtime_gemini_execution.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_runtime_model_controls.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_runtime_model_controls.py) | Да | 13 | 13 / 0 / 0 | — | — |
| [tests/test_runtime_operation_boundaries.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_runtime_operation_boundaries.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_runtime_process_writers.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_runtime_process_writers.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_runtime_settings_store.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_runtime_settings_store.py) | Да | 16 | 16 / 0 / 0 | — | — |
| [tests/test_runtime_specialized_callers.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_runtime_specialized_callers.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_runtime_usage_metrics.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_runtime_usage_metrics.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_search_services.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_search_services.py) | Да | 9 | 9 / 0 / 0 | — | — |
| [tests/test_specialized_process_policies.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_specialized_process_policies.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_typed_provider_payloads.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_typed_provider_payloads.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_update_processor.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_update_processor.py) | Да | 12 | 12 / 0 / 0 | — | — |
| [tests/test_vision_intent.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_vision_intent.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_voice_engine.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_voice_engine.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_voice_intent.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_voice_intent.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_voice_race_cleanup.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_voice_race_cleanup.py) | Да | 6 | 6 / 0 / 0 | — | AI-MEM-010 |

## Платформа, обработчики, web и инфраструктура

Файлов: **154**; собранных случаев: **1674**; все прочитаны.

| Файл | Прочитан | Собрано | Локальный unit | CI integration | Находки |
| --- | --- | ---: | --- | ---: | --- |
| [tests/test_account_erasure.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_account_erasure.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_admin_alerts.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_admin_alerts.py) | Да | 14 | 14 / 0 / 0 | — | — |
| [tests/test_analytics.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_analytics.py) | Да | 17 | 17 / 0 / 0 | — | — |
| [tests/test_api_logger_request_id.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_api_logger_request_id.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_audit_fixes.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_audit_fixes.py) | Да | 36 | 36 / 0 / 0 | — | PLAT-002 |
| [tests/test_auth_headers.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_auth_headers.py) | Да | 2 | 2 / 0 / 0 | — | PLAT-001 |
| [tests/test_background_tasks.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_background_tasks.py) | Да | 13 | 13 / 0 / 0 | — | — |
| [tests/test_basics.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_basics.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_bot_error_handler.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_bot_error_handler.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_bot_help_catalog.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_bot_help_catalog.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_cache_fallback.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cache_fallback.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/test_cache_ttl.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cache_ttl.py) | Да | 4 | 4 / 0 / 0 | — | PLAT-004 |
| [tests/test_callback_responsiveness_scenario.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_callback_responsiveness_scenario.py) | Да | 1 | 1 / 0 / 0 | — | PLAT-013 |
| [tests/test_callbacks.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_callbacks.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_cb_branches.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cb_branches.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_cb_feedback.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cb_feedback.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/test_cb_fwd_save.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cb_fwd_save.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_cb_models.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cb_models.py) | Да | 9 | 9 / 0 / 0 | — | — |
| [tests/test_cb_navigation.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cb_navigation.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_chat_logic.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_chat_logic.py) | Да | 22 | 22 / 0 / 0 | — | — |
| [tests/test_chat_persistence.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_chat_persistence.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_check_encoding.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_check_encoding.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_chunking.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_chunking.py) | Да | 16 | 16 / 0 / 0 | — | — |
| [tests/test_circuit_breaker.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_circuit_breaker.py) | Да | 17 | 17 / 0 / 0 | — | — |
| [tests/test_circuit_breaker_concurrency.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_circuit_breaker_concurrency.py) | Да | 1 | 1 / 0 / 0 | — | PLAT-013, PLAT-014 |
| [tests/test_cmd_admin.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cmd_admin.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_cmd_conversations.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cmd_conversations.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/test_cmd_models.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cmd_models.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_cmd_reminders.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cmd_reminders.py) | Да | 56 | 56 / 0 / 0 | — | PLAT-009 |
| [tests/test_commands.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_commands.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_concurrency.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_concurrency.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_concurrency_hardening.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_concurrency_hardening.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_config_helpers.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_config_helpers.py) | Да | 39 | 39 / 0 / 0 | — | — |
| [tests/test_consolidation_debounce.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_consolidation_debounce.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_controls_ui.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_controls_ui.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_conversation_handler_warnings.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_conversation_handler_warnings.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_crypto.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_crypto.py) | Да | 14 | 14 / 0 / 0 | — | — |
| [tests/test_dashboard_snapshot.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dashboard_snapshot.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_database_init_order.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_database_init_order.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_database_tavily.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_database_tavily.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_db_seed.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_db_seed.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_decorators.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_decorators.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_decryption_error_handling.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_decryption_error_handling.py) | Да | 5 | 5 / 0 / 0 | — | PLAT-012 |
| [tests/test_dedup.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dedup.py) | Да | 14 | 14 / 0 / 0 | — | — |
| [tests/test_deferred_response.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_deferred_response.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_degradation_recovery.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_degradation_recovery.py) | Да | 18 | 18 / 0 / 0 | — | — |
| [tests/test_dependency_boundaries.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dependency_boundaries.py) | Да | 9 | 9 / 0 / 0 | — | — |
| [tests/test_dependency_container_smoke.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dependency_container_smoke.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_dependency_deploy_scope.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dependency_deploy_scope.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_dependency_environment_check.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dependency_environment_check.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_dependency_frontier.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dependency_frontier.py) | Да | 34 | 34 / 0 / 0 | — | — |
| [tests/test_dependency_frontier_workflow.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dependency_frontier_workflow.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_dependency_license_inventory.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dependency_license_inventory.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_dependency_live_canary.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dependency_live_canary.py) | Да | 14 | 14 / 0 / 0 | — | — |
| [tests/test_dependency_live_canary_workflow.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dependency_live_canary_workflow.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_dependency_metadata.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_dependency_metadata.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_deploy_workflow_config.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_deploy_workflow_config.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_distributed_semaphore.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_distributed_semaphore.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_docs_links.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_docs_links.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_document_cleanup_optimization.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_document_cleanup_optimization.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_document_security.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_document_security.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_documents_parsers.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_documents_parsers.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_documents_repository.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_documents_repository.py) | Да | 17 | 17 / 0 / 0 | — | — |
| [tests/test_env_registry.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_env_registry.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_error_codes.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_error_codes.py) | Да | 61 | 61 / 0 / 0 | — | — |
| [tests/test_errors.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_errors.py) | Да | 39 | 39 / 0 / 0 | — | — |
| [tests/test_external_api_hardening.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_external_api_hardening.py) | Да | 41 | 41 / 0 / 0 | — | — |
| [tests/test_factories.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_factories.py) | Да | 15 | 15 / 0 / 0 | — | — |
| [tests/test_fast_callback_channel.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_fast_callback_channel.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_formatting.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_formatting.py) | Да | 16 | 16 / 0 / 0 | — | — |
| [tests/test_formatting_e2e.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_formatting_e2e.py) | Да | 43 | 43 / 0 / 0 | — | PLAT-006 |
| [tests/test_group_chat.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_group_chat.py) | Да | 9 | 9 / 0 / 0 | — | PLAT-002 |
| [tests/test_heartbeat.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_heartbeat.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_image_quota_reservation.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_image_quota_reservation.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_image_utils.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_image_utils.py) | Да | 9 | 9 / 0 / 0 | — | — |
| [tests/test_inline.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_inline.py) | Да | 20 | 20 / 0 / 0 | — | — |
| [tests/test_inline_expired_query.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_inline_expired_query.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_integration.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_integration.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_integration_flow.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_integration_flow.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_integration_flows.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_integration_flows.py) | Да | 18 | 18 / 0 / 0 | — | — |
| [tests/test_io_handlers.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_io_handlers.py) | Да | 3 | 3 / 0 / 0 | — | PLAT-005, PLAT-014 |
| [tests/test_json_utils.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_json_utils.py) | Да | 21 | 21 / 0 / 0 | — | — |
| [tests/test_keyboards.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_keyboards.py) | Да | 26 | 26 / 0 / 0 | — | — |
| [tests/test_keys_thundering_herd.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_keys_thundering_herd.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_local_bot_api_release_contract.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_local_bot_api_release_contract.py) | Да | 2 | 2 / 0 / 0 | — | PLAT-007 |
| [tests/test_long_messages.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_long_messages.py) | Да | 9 | 9 / 0 / 0 | — | — |
| [tests/test_long_wait_recovery.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_long_wait_recovery.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_menus.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_menus.py) | Да | 18 | 17 / 0 / 0 | 1 | PLAT-001 |
| [tests/test_messages.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_messages.py) | Да | 9 | 9 / 0 / 0 | — | — |
| [tests/test_messages_chunking.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_messages_chunking.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_metrics_integration.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_metrics_integration.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_metrics_middleware.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_metrics_middleware.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/test_metrics_repo.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_metrics_repo.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_metrics_snapshot.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_metrics_snapshot.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/test_migration_invariants.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_migration_invariants.py) | Да | 15 | 15 / 0 / 0 | — | — |
| [tests/test_migration_serialization.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_migration_serialization.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_miniapp_authorization.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_miniapp_authorization.py) | Да | 24 | 24 / 0 / 0 | — | — |
| [tests/test_model_selector.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_model_selector.py) | Да | 18 | 18 / 0 / 0 | — | — |
| [tests/test_models_repo.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_models_repo.py) | Да | 13 | 13 / 0 / 0 | — | — |
| [tests/test_mutation_smoke.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_mutation_smoke.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_network.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_network.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_perf_db_messages.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_perf_db_messages.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_phase3_features.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_phase3_features.py) | Да | 17 | 17 / 0 / 0 | — | — |
| [tests/test_reader_ssr.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_reader_ssr.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_reader_utils.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_reader_utils.py) | Да | 23 | 23 / 0 / 0 | — | — |
| [tests/test_redis_queue.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_redis_queue.py) | Да | 21 | 21 / 0 / 0 | — | PLAT-008 |
| [tests/test_repo_chats.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_repo_chats.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_repo_conversations.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_repo_conversations.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_repos_conversations.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_repos_conversations.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_repos_keys.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_repos_keys.py) | Да | 14 | 14 / 0 / 0 | — | PLAT-011 |
| [tests/test_repos_roles.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_repos_roles.py) | Да | 10 | 10 / 0 / 0 | — | — |
| [tests/test_repos_users.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_repos_users.py) | Да | 11 | 11 / 0 / 0 | — | — |
| [tests/test_request_id_headers.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_request_id_headers.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_resilience.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_resilience.py) | Да | 23 | 23 / 0 / 0 | — | — |
| [tests/test_response_coordinator.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_response_coordinator.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/test_response_presentation.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_response_presentation.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_response_tags.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_response_tags.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_rls_policies.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_rls_policies.py) | Да | 18 | 18 / 0 / 0 | — | — |
| [tests/test_role_conversation_metrics.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_role_conversation_metrics.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_roles_menu.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_roles_menu.py) | Да | 9 | 9 / 0 / 0 | — | — |
| [tests/test_save_conversation.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_save_conversation.py) | Да | 6 | 6 / 0 / 0 | — | — |
| [tests/test_scheduled_briefs.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_scheduled_briefs.py) | Да | 15 | 15 / 0 / 0 | — | — |
| [tests/test_security.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_security.py) | Да | 34 | 34 / 0 / 0 | — | — |
| [tests/test_security_headers.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_security_headers.py) | Да | 3 | 3 / 0 / 0 | — | PLAT-001 |
| [tests/test_semaphore_invariants.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_semaphore_invariants.py) | Да | 7 | 7 / 0 / 0 | — | PLAT-010 |
| [tests/test_send_long_message.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_send_long_message.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_settings_repo.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_settings_repo.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_smoke.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_smoke.py) | Да | 12 | 12 / 0 / 0 | — | — |
| [tests/test_stage_indicators.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_stage_indicators.py) | Да | 15 | 15 / 0 / 0 | — | — |
| [tests/test_start_polling_kwargs.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_start_polling_kwargs.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_state_lifecycle.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_state_lifecycle.py) | Да | 19 | 19 / 0 / 0 | — | — |
| [tests/test_system_status.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_system_status.py) | Да | 1 | 1 / 0 / 0 | — | PLAT-001 |
| [tests/test_task_queue.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_task_queue.py) | Да | 12 | 12 / 0 / 0 | — | — |
| [tests/test_taskmanager_bounded.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_taskmanager_bounded.py) | Да | 2 | 2 / 0 / 0 | — | PLAT-014 |
| [tests/test_telegram_cloud_guard.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_telegram_cloud_guard.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_telegram_renderer.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_telegram_renderer.py) | Да | 13 | 13 / 0 / 0 | — | — |
| [tests/test_template_accessibility.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_template_accessibility.py) | Да | 7 | 7 / 0 / 0 | — | — |
| [tests/test_text_format.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_text_format.py) | Да | 45 | 45 / 0 / 0 | — | — |
| [tests/test_text_format_aaa.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_text_format_aaa.py) | Да | 30 | 30 / 0 / 0 | — | — |
| [tests/test_text_split_performance.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_text_split_performance.py) | Да | 2 | 2 / 0 / 0 | — | — |
| [tests/test_thinking_classifier.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_thinking_classifier.py) | Да | 57 | 57 / 0 / 0 | — | — |
| [tests/test_time_utils.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_time_utils.py) | Да | 3 | 3 / 0 / 0 | — | — |
| [tests/test_timeout_smoke.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_timeout_smoke.py) | Да | 4 | 4 / 0 / 0 | — | — |
| [tests/test_token_budget.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_token_budget.py) | Да | 14 | 14 / 0 / 0 | — | — |
| [tests/test_tracing.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_tracing.py) | Да | 8 | 8 / 0 / 0 | — | — |
| [tests/test_typed_exceptions.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_typed_exceptions.py) | Да | 20 | 20 / 0 / 0 | — | — |
| [tests/test_unified_call_path.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_unified_call_path.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_user_facing_errors.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_user_facing_errors.py) | Да | 9 | 9 / 0 / 0 | — | — |
| [tests/test_user_state_locks.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_user_state_locks.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_waiting_facts.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_waiting_facts.py) | Да | 6 | 6 / 0 / 0 | — | PLAT-003 |
| [tests/test_web_controls.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_web_controls.py) | Да | 5 | 5 / 0 / 0 | — | — |
| [tests/test_web_reader.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_web_reader.py) | Да | 1 | 1 / 0 / 0 | — | — |
| [tests/test_web_security.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_web_security.py) | Да | 14 | 14 / 0 / 0 | — | PLAT-001, PLAT-015 |
| [tests/test_webhook_dedupe.py](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_webhook_dedupe.py) | Да | 3 | 3 / 0 / 0 | — | — |
