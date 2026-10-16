# Приоритетное покрытие тестами — 2026-10-15

Добавлены **28 новых тестовых случаев** для четырёх критичных контрактов.
Проверки воспроизвели три дефекта существующего поведения; минимальные исправления
закреплены регрессионными тестами. Независимое ревью одобрило изменения без замечаний.

Результаты относятся к незакоммиченному рабочему дереву поверх
`68489459ecf7660eb2a23da474b52c430cb9be2f`. Commit, push, PR, deployment и миграции
не выполнялись. Предыдущие датированные отчёты остаются историческими.
Область и ограничения зафиксированы в [плане](superpowers/plans/2026-10-15-priority-test-coverage.md).

| Область | Новые случаи | Наблюдаемый контракт |
| --- | ---: | --- |
| [Атрибуты источников графа](../tests/test_memory_graph_source_attributes.py) | 8 | Exact/canonical-alias связи, оба порядка кандидатов, пересекающиеся источники и повторы одного источника сохраняют собственные атрибуты provenance; aggregate edge получает max weight / OR core без дублирования source rows. |
| [Отмена задач памяти пользователя](../tests/test_memory_user_cancellation_ownership.py) | 6 | Обычный drain, однократная/повторная отмена вызывающей операции и успешный/ошибочный async cleanup: завершение дочернего finally до выхода, первая причина отмены, очистка registry и сохранение задач другого пользователя. |
| [Сбой admission перед запуском worker](../tests/test_message_admission_failures.py) | 8 | NetworkError и event-controlled cancellation во время placeholder для voice/text/photo/draw освобождают busy reservation; provider/workload не стартует, а следующий валидный запрос получает настоящий canonical lock и завершается. |
| [Видимость Long Read](../tests/test_long_read_visibility_contracts.py) | 6 | Невидимая Reader/Telegraph ссылка ведёт к следующему fallback или полному split; receipt, видимый текст, message IDs и финальные actions согласованы; отмена останавливает fallback, cold mirror начинается после видимой Reader ссылки. |

Внешние SQL/Telegram/cache/publication операции заменены синтетическими doubles.
Проверяемые владельцы остаются настоящими: graph writer, registry/cancellation
helper, message routing/state locks/debounce/heartbeat/TaskManager и delivery
facade/transport/renderer. Admission workload doubles ограничивают этот набор
границей запуска worker, не заявляя выполнение провайдера.

В [memory_graph_writer.py](../app/repos/memory_graph_writer.py) объединённые
атрибуты ребра ранее попадали в каждый source snapshot. Теперь writer строит
snapshots из исходных кандидатов и объединяет weight/core только для одинаковой
пары edge/source. RED показал, что слабый источник получал чужие `.9/True` вместо
своих `.2/False`; после исправления восемь новых случаев и соседние writer,
consolidation, extraction и database-event проверки проходят. SQL projections,
tenant predicates и владение транзакцией сохранены.

В [memory_autosave.py](../app/repos/memory_autosave.py) bare gather при прямой
отмене задач пользователя позволял отмене вызывающей операции прервать async
finally дочерней задачи. RED проверял именно прерывание cleanup. Прямой путь
теперь использует уже существующий shielded `_cancel_and_await`; его семантика
сохранена. Cleanup ожидается до распространения первой причины отмены.

В [messages.py](../app/handlers/messages.py) три узких guard вокруг Telegram
placeholder await освобождают reservation при ошибке или cancellation до передачи
cleanup background worker. Все восемь сочетаний ветки и сбоя сначала падали
на настоящем `is_processing` leak; каждый GREEN дополнительно проверяет следующий
успешно допущенный запрос. Long Read runtime менять не потребовалось: шесть
новых случаев закрепляют уже правильное поведение.

| Проверка | Результат |
| --- | --- |
| Graph baseline / новые и baseline / соседние модули | 10 / 18 / 55 passed |
| Memory baseline / новые и baseline / соседние модули | 19 / 25 / 57 passed |
| Admission baseline / новые / соседние / после format | 3 / 8 / 83 / 11 passed |
| Renderer baseline / новые / соседние модули | 13 / 6 / 38 passed |
| Независимый запуск четырёх новых модулей | 28 passed |
| Первый общий прогон с branch coverage | 4611 passed, 1 failed, 7 deselected; 729.301 s recorder duration |
| Финальный общий прогон без coverage | **4612 passed, 0 failed, 0 skipped**, 7 deselected; 466.425 s recorder duration |

Focused наборы перекрываются; их числа не складываются с общим прогоном.
Все 28 новых случаев входят в оба общих прогона и имеют успешные setup/call/teardown.
Финальный общий запуск включает **118 passing browser-marked cases**, в том числе
Node/AudioWorklet проверки. Датированный runner очищает
`GEMAIBOT_REQUIRE_BROWSER_TESTS`; browser evidence здесь устанавливается фактическими
passing cases, а не эффективным mandatory flag.

Единственный сбой первого общего прогона:
`tests/test_ci_workflow_config.py::test_browser_job_selection_matches_all_discovered_browser_modules`.
Traceback показывает `subprocess.TimeoutExpired`: две полные дочерние коллекции
делят deadline 25 s, а второй осталось 1.6315 s. Это не assertion о несовпадении
inventory. Отдельный повтор прошёл за 24.44 s; последующий общий прогон без
coverage прошёл целиком. Сам тест, selector и его лимиты не менялись. Первый
неуспешный прогон сохранён отдельно и не объявляется успешным измерением suite.

| Измерение Python `app` + `bot.py` | Covered / total | Доля |
| --- | ---: | ---: |
| Statements / executable lines | 31762 / 44647 | 71.1403% |
| Branches | 7885 / 13078 | 60.2921% |
| Combined coverage.py measure | 39647 / 57725 | 68.6825% |

| Source | Statements | Branches |
| --- | ---: | ---: |
| `app/repos/memory_graph_writer.py` | 95.00% | 84.00% |
| `app/repos/memory_autosave.py` | 82.86% | 67.86% |
| `app/handlers/messages.py` | 61.18% | 55.97% |
| `app/response_delivery/renderer.py` | 85.53% | 73.44% |

Coverage взято из первого инструментированного прогона с явно указанным CI
timeout. Оба общих прогона проверяли один неизменный snapshot: **756 выбранных
source/test/config и прежних coverage/corpus файлов** совпали по SHA-256 до и после
выполнения. Общий прирост процентов относительно старых отчётов не заявляется:
их код, тестовый состав и denominator отличаются. Python coverage не измеряет
исполнение SQL triggers или покрытие JavaScript.

Все сабагенты использовали `gpt-6.1-sol`: `high` для admission/delivery и `xhigh`
для memory cancellation и независимого ревью. Reviewer проверил runtime diff,
четыре новых модуля, соседние owners/ADRs и RED evidence, затем отдельно запустил
28 новых случаев: **APPROVE**, 0 Critical/Important/Minor findings.
Locked Ruff check/format (734 Python files), Mypy (288 source files), environment
registry (111 names, 0 changes), UTF-8, current/new-document links и
`git diff --check` прошли.

Воспроизведение описано в [CONTRIBUTING](../CONTRIBUTING.md). Все запуски использовали
Python 3.14.3 и uv 0.12.6 с locked dependencies. Для каждого запуска задавался
свой `GEMAIBOT_AUDIT_OUTPUT_DIR`:

```powershell
uv run --locked python -X utf8 docs/test-coverage/2026-10-09/offline_audit.py focus tests/ --ignore=tests/integration -m "not integration"
uv run --locked python -X utf8 docs/test-coverage/2026-10-09/offline_audit.py unit
```

Доказательства: [компактный JSON](test-coverage/2026-10-15-priority/evidence.json)
с точными новыми nodeid, phase outcomes, фактическими arguments, coverage totals,
source/evidence hashes и всеми focused исходами;
[приложение](test-coverage/2026-10-15-priority/README.md).
Сырые синтетические логи, RED/GREEN, полный hash snapshot и recorder outputs
сохранены в ignored `.superpowers/sdd/2026-10-15-priority-coverage/`.
Два предварительных graph-neighbor вызова указали отсутствующие модули и
собрали ноль тестов (exit 4); исправленный final selection дал 55 passes.

Repository `.env` недоступен offline runner, настройки синтетические;
запрещённых Python socket attempts — **0** в обоих общих прогонах.
Это Python guards, не OS sandbox для дочерних процессов. PostgreSQL, Redis,
collector integration, live Telegram/provider/Telegraph, deployment и миграции
в этом этапе не выполнялись. Graph tests доказывают настоящие writer payloads;
delete/recomposition через реальные SQL triggers требует disposable PostgreSQL.
Ранее сохранённые source rows этим изменением ретроспективно не переписываются.

Следующие выделенные пробелы: delayed cleanup старой задачи при edited-message
supersession и cancellation во время direct-intent await до placeholder.
Это отдельные сценарии для последующего воспроизведения, не подтверждённые
новыми тестами данного этапа. Concurrent cancellation owners и cleanup без
завершения также не входят в проверенную гарантию одного caller owner.
