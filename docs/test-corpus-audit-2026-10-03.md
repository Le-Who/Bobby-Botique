# Аудит корпуса тестов — 2026-10-03

Проверяемый checkout: `425563b4178f1ff63c3589e772e13c4d6485c630`, ветка
`vps_testai`. Это датированный отчёт о тестах и выполненных проверках, а не
подтверждение всех свойств работающего бота и не инструкция реализовать весь
перечисленный backlog.

Все 43 записи позднее исправлены по явному запросу пользователя. Результаты
сохранены в [отдельном отчёте об исправлениях](test-corpus-fixes-2026-10-03.md)
и [JSON-доказательствах](test-corpus-fixes-evidence-2026-10-03.json). Ниже остаётся
исходный baseline `425563b4`; прежние пропуски и контрпримеры описывают именно его.

Исходное падение интеграции устранено; исправление и блокировавшее CI обновление
двух зависимостей отправлены в ветку. Все семь задач
[CI этого checkout](https://github.com/Le-Who/Bobby-Botique/actions/runs/37084408293)
завершились успешно. Аудит обнаружил отдельные пробелы в тестовом доказательстве:
зелёный выбранный набор не означает, что каждый заявленный сценарий действительно
выполнен и проверен.

[Автоматический деплой](https://github.com/Le-Who/Bobby-Botique/actions/runs/37084572055)
для того же `425563b4` завершился успешно. Это подтверждённый результат workflow;
живой диалог Telegram/провайдера и полная доставка логов этим аудитом не измерялись.

## Исправленный root cause и блокер деплоя

В `db_conn_with_key` обеих DB-фикстур SHA-256 обрезался до 16 символов. Рабочий
код резервирования квоты использует полный 64-символьный SHA-256 ключа. Поэтому
вставка в `key_usage` ссылалась на отсутствующий `api_keys.key_hash`, нарушала
внешний ключ и переводила транзакцию в aborted state. Последующие ошибки запросов
были следствием первой ошибки.

- `abe950d24ab3fb7ae9b9d8c346ced058b6ebb0c7`:
  [integration fixture](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/conftest.py),
  [E2E fixture](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/conftest.py) и независимый известный SHA-256 в
  [регрессионной проверке](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_test_environment_safety.py).
  До исправления оба параметризованных случая воспроизводили несовпадение
  16/64; после исправления профильный набор прошёл.
- `425563b4178f1ff63c3589e772e13c4d6485c630`:
  в [uv.lock](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/uv.lock) изменены только `pypdf` `6.16.2 → 6.19.0` и
  `urllib3` `2.7.0 → 2.8.0`. Это отдельный blocker задачи Audit Production
  Dependencies; манифест и остальные версии не обновлялись.

Исходный `test_e2e_happy_path_conversation` теперь проходит на реальном
изолированном PostgreSQL в CI. Обновлённый production dependency graph прошёл
`pip-audit` с результатом `No known vulnerabilities found` на дату проверки.

## Охват и метод

Проверены все **352 tracked Python-файла** под `tests/`: **69 846 строк**,
включая фикстуры, фабрики и вспомогательные файлы. Других tracked расширений в
этом каталоге нет. Pytest собирает **3 786 случаев в 343 тестовых модулях**;
65 случаев имеют маркер `integration`. Параметризация означает, что количество
случаев отличается от числа функций. AST-имена `test_*` не использовались как
точное число тестов: среди них есть фикстуры `test_db_url` и `test_user_id`.

Выполнены чтение полных тел тестов и фикстур, сопоставление с текущими границами
приложения, проверка выбранных assertions на ложный успех, отдельные офлайн
контрпримеры и запуск корпуса. Автоматические сигналы — короткий `sleep`,
`sys.modules`, чтение исходника, отсутствие обычного `assert` — использовались
для навигации. Они сами по себе не объявлялись дефектами. Проверки через
`assert_awaited*`, `pytest.raises` и явно заданные структурные контракты могут
быть корректными.

Для каждого файла сохранены статус чтения, число собранных случаев и связанные
находки в [полном реестре](test-corpus-inventory-2026-10-03.md). Машинные
доказательства и пояснения находятся в
[JSON-приложении](test-corpus-audit-evidence-2026-10-03.json).

Проверенный инструментарий: Python 3.14.3, uv 0.12.6, pytest 9.1.1,
pytest-asyncio 1.4.0, pytest-xdist 3.8.0, pytest-timeout 2.4.0, Ruff 0.15.2.
Оценка опирается на эти установленные версии и действующие API, а не на старые
советы о ручном управлении event loop. Массовое обновление инструментов для
проведения аудита не потребовалось.

## Фактически выполненные проверки

| Проверка | Результат и граница доказательства |
| --- | --- |
| Красный/зелёный контракт DB-фикстур | Оба новых случая падали до исправления; профильный набор после исправления: 59 passed |
| Полный локальный unit/E2E, serial, timeout 30 | 3 688 passed, 33 skipped, 2 deselected; 295,35 с; до обновления двух зависимостей |
| Последний CI unit/E2E, обычный xdist | 3 688 passed, 33 skipped; 46,21 с; checkout `425563b4` |
| Последний CI PostgreSQL/pgvector, serial | 65 passed, 3 721 deselected; 18,80 с; исходный chat E2E passed |
| Три проверенных DB-independent E2E-файла, снят только ошибочный DB-skip | 23 passed, **3 failed**; 2,94 с; подтверждение HAR-001, не штатный зелёный прогон |
| Memory query expansion timeout отдельно, Windows, текущий lock | **1 failed** на `cancelled.is_set()`; 1,74 с; в полном прогоне этот случай прошёл |
| После обновления lock: parser/dependency boundary/environment/metadata smoke | 32 passed |
| `uv lock --check`, `uv pip check`, production `pip-audit` | Успех; известные уязвимости в экспортированном production graph не обнаружены |
| Ruff check, Ruff format check, Mypy | Успех локально и в последнем CI |
| Production image build, offline container smoke | Успех в последнем CI; локального Docker нет |
| Vendor observability configuration/readiness | Успех в последнем CI; это не проверка полного пути доставки логов |
| Автоматический Deploy Telegram Bot | Success на `425563b4`, run `37084572055` |

Локальный прогон использовал:

```text
uv run --locked pytest tests/ --ignore=tests/integration -m "not integration" --override-ini="addopts=" --timeout=30 -q
```

Все **33 пропуска** объяснены: 26 DB-independent E2E из-за HAR-001,
2 DB-теста подписок из-за отсутствующего `testcontainers` и
5 браузерных случаев natal UI из-за отсутствующего Playwright. Эти случаи не
засчитаны как пройденные. Живые Telegram/Gemini/другие провайдеры, полноценное
логирование на VPS и optional container/browser пути локально не проверялись.

Unit-запуск использовал принудительные тестовые credentials из root conftest.
Это не гарантия отсутствия любого I/O: AI-MEM-007 и PLAT-003 обнаружили
незапланированные DB-boundaries, HAR-005 — отдельно не замоканный memory SDK
в интеграционном E2E. Поэтому результат не назван проверкой живых сервисов.

## Критерии и актуальные источники

Использован Context7 для документации pytest, pytest-asyncio и pytest-xdist;
ключевые положения сверены с первичными документациями на 2026-10-03.

- Fixture должна владеть очисткой сразу после создания ресурса. Ошибка до
  `yield` не выполняет teardown этой yield-фикстуры; нужны соответствующие
  `try/finally`/finalizer, а cleanup не должен маскировать причину сбоя.
  [Pytest: fixtures](https://docs.pytest.org/en/stable/how-to/fixtures.html).
- Тест должен быть независим от остаточного состояния и случайной скорости
  машины. Для конкуренции предпочтительны управляемые события и достаточный
  safety timeout; для числовых расчётов — обоснованные допуски.
  [Pytest: flaky tests](https://docs.pytest.org/en/stable/explanation/flaky.html).
- Патчи среды, объектов и реестра модулей должны иметь определённое время
  жизни. Восстановление `sys.modules` не отменяет уже выполненный reload и не
  переписывает ссылки, импортированные другими модулями.
  [Pytest: monkeypatch](https://docs.pytest.org/en/stable/how-to/monkeypatch.html).
- Async fixture и ресурсы должны использовать совместимые event loop scopes.
  `asyncio_mode=auto` поддерживает async `@pytest.fixture`; обязательная массовая
  замена декораторов или переход в strict mode здесь не обоснованы.
  [Pytest-asyncio: concepts](https://pytest-asyncio.readthedocs.io/en/stable/concepts.html).
- Session fixture создаётся отдельно в каждом worker; `xdist_group` определяет
  совместное размещение, но не восстанавливает глобальное состояние внутри
  worker. Для общих внешних ресурсов требуется явная изоляция.
  [Pytest-xdist: how-tos](https://pytest-xdist.readthedocs.io/en/stable/how-to.html).

Контрпример считается доказательством конкретного пробела assertions, а не
найденной неисправностью production. Например, возможность пройти тест после
замены обработчика на no-op доказывает слабость этого теста; она не означает,
что рабочий обработчик сейчас является no-op. Мок внешней границы уместен;
подмена самой проверяемой логики собственной реализацией в тесте требует другого
названия и отдельного покрытия реальной логики.

## HAR: инфраструктура, E2E, SQL и observability

**HAR-001, P1 — 26 DB-independent E2E не выполняются ни одним CI job.**
[E2E collection hook:32](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/conftest.py) пропускает весь каталог без
`TEST_DATABASE_URL`. Unit job эту переменную не задаёт; integration job выбирает
только `-m integration`. Три DB-free файла без этого маркера остаются вне обоих
выборов. В контролируемом офлайн запуске их 26 случаев дали 23 passed и три
падения Opencode cascade. Их
[_make_use_case:51](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/test_opencode_cascade.py) создаёт `MagicMock`
без async `reserve_key_usage`, который теперь await-ит router. Следующий шаг:
skip только по реальной DB-зависимости, включить эти E2E в unit job и обновить
double квоты с проверкой фактического резервирования.

**HAR-002, P2 — часть «repository integration» проверяет копию SQL.**
[Roles:121](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_repos_roles.py), `test_repos_chats`,
`conversations`, `keys`, `user_stats`, `users`, `test_metrics_jsonb` и
`test_db_repos` выполняют собственные SQL-строки. Это полезные schema/driver
smoke-проверки, но изменение SQL или tenant-фильтра в `app/repos` может не
повлиять на них. Следующий шаг: сохранить явно названный schema smoke и добавить
вызовы реальных repo API, чужой user ID, реальные quota identity и транзакции.

**HAR-003, P2 — lifecycle assertion проверяет локальный литерал.**
[Incident scenarios:23](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/observability/test_incident_scenarios.py)
создаёт `[start, finish]` и считает `finish`; пять параметров не вызывают
production emitter/owner. Следующий шаг: пройти реальные success/failure/cancel
пути и проверить корреляцию и единственное terminal event. Соседние redaction
и benchmark проверки содержательны.

**HAR-004, P2 — webhook E2E выполняет тестовый обработчик.**
[Fixture:55](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/test_webhook_lifecycle.py) регистрирует собственный
Quart handler с `process_update`. Текущий [bot handler:494](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/bot.py) использует
проверку заголовка, дедупликацию и `update_queue`; изменение этих путей не
наблюдается тремя тестовыми route-сценариями. Следующий шаг: фактическая
регистрация с boundary doubles или явная классификация как framework smoke.
Health case использует реальный endpoint; отдельные актуальные webhook-тесты
в корпусе существуют.

**HAR-005, P2 — chat E2E не полностью изолирует provider I/O.**
[Patch list:70](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/test_chat_happy_path.py) заменяет routed ответ,
но [memory repo](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/app/repos/memory.py) вызывает Gemini для query expansion
и embedding отдельно. Первоначальное падение действительно дошло до квоты
`memory.expand`. Dummy key не является network mock. Следующий шаг: сохранить
реальную DB/quota границу, стабилизировать специализированные provider calls
и проверить отсутствие незапланированной сети в офлайн наборе.

**HAR-006, P2 — DB fixture может чинить схему и потерять cleanup при setup error.**
[Integration fixture:65](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/conftest.py) и
[E2E fixture:58](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/e2e/conftest.py) устанавливают codec/начинают транзакцию
до cleanup-owned `try`, добавляют compatibility columns и подавляют teardown
exceptions. Это маскирует отсутствие соответствующих мигрированных полей на
границе самого теста; CI migration/schema gates дают отдельную защиту.
Следующий шаг: cleanup сразу после connect, явный rollback своей здоровой
транзакции, bounded cleanup повреждённого connection с диагностикой и тесты
только на мигрированной схеме.

**HAR-007, P2 — последовательность сообщений реализована внутри теста.**
[Concurrency:105](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/integration/test_concurrency_locks.py) создаёт
`mock_handler_task`, сам захватывает lock и вызывает локальные функции.
Удаление lock из рабочего message handler не ломает эту проверку. Следующий
шаг: два реальных запроса с `Event`, проверка порядка и независимости разных
пользователей. Соседний busy callback действительно вызывает production handler.

**HAR-008 / PROD-006, P2 — optional DB-тесты подписок отсутствуют в selection.**
[Subscriptions:61](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_horoscope_subscriptions.py) и второй тест
используют необязательный `testcontainers`, не имеют `integration`-маркера и
пропускаются в текущем окружении. [Container fixture:51](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/fixtures/db_container.py)
не восстанавливает env/settings, создаёт singleton pool в закрываемом
`asyncio.run` loop и использует PG16 вместо CI PG17. Активированный контейнерный
путь не запускался. Следующий шаг: использовать существующий isolated PG job
или отдельное provisioned окружение с корректным pool/loop lifecycle. Два ID
описывают один пересекающийся пробел, а не две независимые DB-регрессии.

## PROD: игровые и natal сценарии

| ID | Проверенный пробел | Следующий шаг |
| --- | --- | --- |
| PROD-001, P2 | [WebSocket:44](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_game_websocket.py): `/webapp/ws` отсутствует; два negative case ловят любой `Exception` и допускают даже принятый frame | Реальный `/webapp/game/ws`, конкретный disconnect/status и отказ при неожиданном исходе |
| PROD-002, P2 | [Game cache:286](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_game_cache.py): «перезапуск» заменяет обе real persist/load функции собственным JSON round trip | Реальные функции с `tmp_path`, очистка только process cache и проверка файлового payload/topic isolation |
| PROD-003, P2 | [Daily image:65](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_crocodile_optimizations.py): отсутствующие patch targets с `create=True`, строка вместо `date`, подавленный `AttributeError`, `assert True` при нуле вызовов | Реальный provider import boundary, `date`, актуальный result fake и безусловные assertions вызова/kwargs |
| PROD-004, P2 | [Trivia:865](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_trivia.py) и [Croc models:123](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_croc_models.py): no-save проверяется через старый `set_global_setting`, тогда как новый model writer использует runtime store | Проверять `save_primary_model`/`save_croc_model`, сохраняя отдельные проверки delivery setting |
| PROD-005, P3 | [Daily debounce:501](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_crocodile.py): 2,2 с ожидания при debounce 2,0 с и cleanup только после успешного assertion | Управляемое завершение через событие, подходящий safety timeout и unconditional task/state cleanup |
| PROD-006, P2 | Два DB-теста подписок: пересечение HAR-008 | См. HAR-008 |
| PROD-007, P2 | [Natal service:150](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_service.py): запрет публикации выражен `AssertionError` в fake, но service ловит его и возвращает `None`; результат устраивает тест | Отдельно `assert_not_awaited`/счётчик вызовов после вызова service |
| PROD-008, P2 | [Natal LLM:775,848](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_llm.py): assertions repair prompt находятся внутри fake; production ловит их и возвращает fallback, который подходит итоговым проверкам | Проверить captured requests после service и отличительный текст repaired ответа, исключив fallback |

## AI-MEM: провайдеры, медиа и память

| ID | Проверенный пробел и способ подтверждения | Следующий шаг |
| --- | --- | --- |
| AI-MEM-001, P2 | [Memory timeout:25](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_memory_query_expansion_timeout.py): 10 мс включают policy/snapshot/prompt/quota работу до SDK; отдельно воспроизводится `cancelled.is_set() == False`, полный тёплый прогон проходит | Отдельно проверить timeout до SDK и отмену начавшегося SDK; событие входа, детерминированная policy resolution и контролируемый бюджет |
| AI-MEM-002, P2 | [Voice privacy:57](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_msg_voice_privacy.py): итоговое `active is False` равно начальному состоянию; неизменённый тест прошёл с целиком no-op pipeline | Требовать enter/exit lease и один ASR await, наблюдать ASR внутри lease; denied/stale generation не должен вызывать ASR |
| AI-MEM-003, P2 | [Provider hedge:231](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_provider_model_hedge.py): ожидание до 200 мс допускает и общий 50 мс бюджет, и его перезапуск; неизменённый тест прошёл при actual router mutation в памяти | Контролируемый clock/remaining timeout либо ответ между исходным и ошибочно продлённым дедлайнами |
| AI-MEM-004, P2 | [Router multimodal:189](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_provider_router_integration.py): `use_openrouter is False OR model == заданная Gemini` допускает неверный provider override; actual branch mutation на `True` прошла | Безусловно проверить `use_openrouter=False` в реальном вызове resolver и конфигурацию, где forcing существенен |
| AI-MEM-005, P2 | [Graph regression:699](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_ai_chat.py): fake возвращает два элемента вместо трёх; [compression:257](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/app/context/compression.py) ловит распаковку, graph branch обходится | Актуальная тройка с source passages, фактическое graph содержимое/stats в provider request, отсутствие скрытого отказа retrieval |
| AI-MEM-006, P2 | [Agent optimization:1](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_agent_optimization.py): три теста выполняют локальную копию исторического алгоритма, без app imports/calls | Проверять текущую production сборку контекста или удалить исторические проверки после сверки замены |
| AI-MEM-007, P2 | [Router:29](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_provider_router_integration.py) и [media policy:232](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_media_process_policies.py): durable key-health manager остаётся настоящим; наблюдалась отказавшая попытка соединения с dummy localhost DB | Recording fake health manager с assertions переходов; реальные DB операции только в isolated integration |
| AI-MEM-008, P2 | [Oversized summary:136](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_context_summarizer.py): no-egress проверяет старую `_get_ai_response_with_routing`, текущий summarizer вызывает `execute_text_process` | Проверять текущий provider boundary и отказ refine/callback при oversized input |
| AI-MEM-009, P2 | [Parallel tools:403](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_agentic_improvements.py): мгновенные mocks и итоговые call counts одинаковы при последовательном исполнении | Два event-gated tool fake: оба начали работу до освобождения любого; затем результат и cleanup |
| AI-MEM-010, P2 | [Media deadlines:36](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_media_chain_deadlines.py), [TTS:80](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_voice_race_cleanup.py), [Croc:163](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_provider_result_processes.py): assertions не отличают продлённый бюджет; ASR actual timeout-scope mutation прошла, TTS/Croc проверены по потоку данных; Croc не доходит до reserve | Контролировать общий остаток бюджета и результат между дедлайнами; для Croc первая попытка должна закончиться внутри бюджета перед reserve |
| AI-MEM-011, P1 | [Live auth:167,175](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_live_audio.py): нет assertion отказа, любой exception поглощается; оба неизменённых тела прошли с accepted socket, возвращающим `connected` | Конкретный disconnect/close 4003 и отсутствие создания provider/session; неожиданные ошибки должны падать |
| AI-MEM-012, P2 | [Live forwarding:523](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_live_audio.py): SDK input mock не проверяется, scripted receive выдаёт audio независимо от input; oracle harness прошёл при нуле SDK awaits | Exact decoded PCM Blob/MIME и activity order, output fake должен зависеть от реально принятого SDK input |

P1 у Live auth — приоритет восстановления регрессионной защиты существенной
границы доступа. Текущая production проверка авторизации присутствует; обход
авторизации этим аудитом не обнаружен. Live oracle проверял достаточность
утверждений, не изменённый production handler. Изменения для проверки router/ASR
выполнялись только в памяти изолированного процесса, рабочие файлы не менялись.

## PLAT: платформа, web, обработчики и ресурсы

| ID | Проверенный пробел и способ подтверждения | Следующий шаг |
| --- | --- | --- |
| PLAT-001, P2 | [System status:37](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_system_status.py), auth/web/menus/security headers: reload существующего модуля под `MagicMock` не отменяется восстановлением `sys.modules`; после успешного narrow probe `app.metrics.time_utils`/`MetricsMiddleware` остались mocks, collector сменился | Attribute patches с fixture scope; import/startup tests в subprocess или полное восстановление объектов и parent bindings; post-teardown identity checks |
| PLAT-002, P2 | [Audit fixes:486](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_audit_fixes.py), [Group chat:18](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_group_chat.py): после reload/reset экспортированные aliases и singleton ссылаются на разные объекты; подтверждено после трёх passed cases | Clean-import smoke в subprocess; reset singleton с восстановлением в `finally`/fixture |
| PLAT-003, P2 | [Waiting facts:30](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_waiting_facts.py): без scoped mock случайная 30% personalization ветка делает DB query; intercepted probe подтвердил один запрос при зелёном тесте | Детерминированные static/personalized варианты с явными DB boundary assertions |
| PLAT-004, P2 | [Cache clear:70](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cache_ttl.py): наполняется отдельный memory cache, module `clear_cache` с Redis `None` сразу возвращает, assertions отсутствуют | Реальный владелец memory cache и assertions очистки; отдельно Redis flush/retry/error контракт |
| PLAT-005, P2 | [Executor:74](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_io_handlers.py): невалидные image bytes, incompatible executor mock, возвращённый `None` игнорируется; проверяется только существование worker symbol | Валидное маленькое изображение, корректный Future/worker boundary, фактический submit/result; отдельные error/timeout cases |
| PLAT-006, P2 | [Formatting:220,264](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_formatting_e2e.py): oracle допускает `<i>my_variable_name</i>` и raw unsupported `<script>`; оба scratch counterexample прошли | Literal identifier, отсутствие italic и unsupported tags, точный escaped текст без удаления evidence регулярным выражением |
| PLAT-007, P2 | [Local Bot API:18](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_local_bot_api_release_contract.py): source index находит import имени вместо awaited call; counterexample без actual release проходит | AST реального awaited call и порядок либо startup fake, записывающий release/webhook действия |
| PLAT-008, P2 | [Redis queue:72](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_redis_queue.py): `eval` double повторяет Lua на Python; изменённый script `return 1` всё ещё получает Python fencing; real isolated Lua eval tests в корпусе не найдены | Сохранить orchestration unit tests и добавить изолированные Redis integrations точных production scripts, ownership/TTL/recovery/namespace |
| PLAT-009, P2 | [Reminders:301,330](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_cmd_reminders.py): plain mock `create_task` не запускает и не закрывает coroutine; в registry остаётся fake task, done callback не исполняется | Реальная owned task под scoped worker mocks с await/cancel либо capture/close coroutine и registry restoration |
| PLAT-010, P2 | [Semaphore:75,153](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_semaphore_invariants.py): cancel допускает обычный exception до acquire; limit-one не измеряет overlap; unchanged cases прошли при `ValueError` до acquire и peak concurrency 3 | Событие успешного acquire, требуемый `CancelledError`, reuse permit; event-controlled overlap и peak == 1 |
| PLAT-011, P2 | [Key cache limit:55](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_repos_keys.py): cache fixture кладёт dict в scalar cache, assertion разрешает и dict, и int; реальный reader по контракту возвращает число или `None` | Scalar 200, точный результат/no-query, DB-to-cache round trip и отдельные None/config варианты |
| PLAT-012, P3 | [Decryption message:65](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_decryption_error_handling.py): entire router отдаёт заранее нужную строку, реальная decryption branch не вызывается | Реальный router с resolution `decryption_failed` либо явное имя delegation test и его call-argument assertions |
| PLAT-013, P3 | [Callback timing:83](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_callback_responsiveness_scenario.py), [Breaker:37](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_circuit_breaker_concurrency.py): жёсткие 150/300 мс и sleep для предположения acquire | Event/barrier для функциональной конкуренции, generous timeout только от зависания; benchmark времени отдельно |
| PLAT-014, P3 | [Task manager:54](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_taskmanager_bounded.py), breaker и IO handlers: часть failure paths теряет task ownership/не закрывает monitor/не удаляет caller-owned temp files | Release/cancel/await в `finally`, `breaker.shutdown`, `tmp_path` либо явный unlink; это замечание о cleanup при регрессии |
| PLAT-015, P2 | [Invalid web header:161](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_web_security.py): `wrong_token` header создан, но не передан в `client.get`; повторно проверяется отсутствие auth | Передать header, проверить конкретный rejection и отсутствие protected work |

Риск wall-clock flakiness в PROD-005/PLAT-013 не выдан за наблюдавшееся падение.
Точное локальное падение воспроизведено у AI-MEM-001. Аналогично, отсутствие
real Redis Lua integration не означает, что текущие Lua-скрипты неисправны.

## Что стоит сохранить

В корпусе есть содержательные тесты; массовая перепись не требуется.
Примеры полезных подходов:

- [Database Tavily](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_database_tavily.py) проверяет реальный writer,
  rollback и сохранение кэша при ошибке, commit до invalidation при успехе.
- [Metrics snapshots](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_metrics_snapshot.py) использует события,
  чтобы доказать поступление новых метрик во время DB I/O, и проверяет
  компенсацию при сбое.
- [Response coordinator](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_response_coordinator.py),
  [Telegram renderer](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_telegram_renderer.py) и delivery events
  проверяют typed terminal outcomes, владение финальными action rows,
  опциональную публикацию и cleanup при отмене.
- [Mini App authorization](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_miniapp_authorization.py) использует
  реальные routes и проверяет границу private work/transaction ownership.
  Это отдельное доказательство; оно не превращает слабые Live auth cases в
  полноценные проверки отказа.
- [Media download](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_media_download.py) и
  [SDK hardening](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_external_api_hardening.py) используют реальные
  transport/type boundaries с `httpx.MockTransport`, без живого провайдера.
- [Daily preparation](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_preparation.py) и
  [daily word locking](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_daily_word_locking.py) проверяют состояние,
  конкуренцию и владение lease/transaction в сценариях подготовки.
- [Natal accuracy](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_natal_accuracy.py) сравнивает реальные расчёты
  с сохранёнными внешними эталонами; handler cases проверяют фактические
  Telegram payload и последовательность ввода.
- [Update processor](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_update_processor.py) проверяет очереди,
  освобождение lock и закрытие незапущенных coroutine через события и
  наблюдаемое состояние.
- [Polling API contract](https://github.com/Le-Who/Bobby-Botique/blob/425563b4178f1ff63c3589e772e13c4d6485c630/tests/test_start_polling_kwargs.py) сопоставляет
  AST реального вызова с signature установленной библиотеки. Это пример
  полезной структурной проверки; чтение source само по себе не является
  дефектом.

## Очерёдность последующих изменений

Зафиксировано **43 записи находок в 59 файлах: 2 P1, 37 P2, 4 P3**.
HAR-008/PROD-006 пересекаются на выборе optional DB-тестов; это явно отмечено,
поэтому число записей не представляется числом независимых runtime bugs.
P1 означает первоочередное восстановление существенной регрессионной защиты,
P2 — пробел поведения/актуальности/изоляции, P3 — менее срочное улучшение.

1. Включить 26 DB-free E2E в правильный CI selection и исправить три устаревших
   quota doubles. Acceptance: все 26 исполняются в unit CI, отсутствуют
   необоснованные DB skips, quota/fallback assertions наблюдают текущий контракт.
2. Усилить отрицательные auth/privacy assertions: Live/game sockets, invalid
   web header, voice lease, oversized summary и no-publication. Acceptance:
   контрпримеры с принятым socket/пропущенным lease/нежелательным provider call
   должны ломать соответствующий тест; current production путь проходит.
3. Восстановить mock contracts: graph 3-tuple, runtime model writers, daily
   image provider/result/date. Заменить собственные SQL/algorithm copies там,
   где заявляется проверка настоящего repo/algorithm.
4. Убрать module/singleton contamination и незапланированное unit I/O; обеспечить
   cleanup coroutine, task, connection и temporary files. Acceptance: identity
   checks после teardown, отдельные и serial/xDist прогоны без остаточного
   состояния и незапланированных обращений к сервисам.
5. Проверить настоящие Redis scripts в изолированном namespace, подключить
   реальные subscription DB cases и определить отдельную provisioned среду
   для обязательных browser regression tests. Отсутствие среды должно быть
   видно как непроверенное покрытие, а не success.
6. Заменить слабые timing/concurrency/deadline oracles на управляемые сценарии;
   повторить конкретные counterexamples и профильные проверки. Полный корпус
   нужен после пересекающих фикстуры/selection изменений; runtime gates не
   нужны для каждого отдельного редакторского уточнения отчёта.

Перечисленные изменения — результаты аудита и acceptance criteria. В этой
задаче исправлены и отправлены исходный DB fixture root cause и необходимый
dependency blocker; остальные тесты и runtime не переписывались в рамках
проверки корпуса. Отчёт/реестр сохранены отдельно как локальные результаты
аудита.
