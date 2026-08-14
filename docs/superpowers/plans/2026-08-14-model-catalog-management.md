# План реализации динамического каталога моделей

## Этап 1. Точный env-каталог

**Файлы:** `app/config.py`, `tests/test_config_helpers.py`.

1. Добавить падающие тесты для динамического `gemini-3.7-flash`, сохранения
   порядка, `none`, unset/blank defaults и точных списков без автоматического
   добавления role-моделей.
2. Ввести общий parser selectable model lists и синтаксическую проверку Gemini
   chat IDs.
3. Убрать использование `CURRENT_GEMINI_MODELS` как allowlist при загрузке и
   нормализации role-моделей.
4. Запустить целевые тесты и привести их к GREEN.

## Этап 2. Env baseline и явный DB override

**Файлы:** `app/repos/settings_repo.py`, `app/repos/models_repo.py`,
`tests/test_models_repo.py`.

1. Тестами определить v2 record, legacy migration, актуализацию env после
   рестарта и reset через удаление DB-записи.
2. Добавить удаление global setting с cache invalidation.
3. Заменить provider `if/else` на явный registry и immutable catalog snapshot.
4. Применять только v2 admin override; legacy list удалить и использовать env.
5. Проверить RED → GREEN только на repository/config тестах.

## Этап 3. Типизированные мутации и Gemini capability validation

**Файлы:** `app/repos/models_repo.py`, `app/providers/gemini.py`,
`tests/test_models_repo.py`, `tests/test_gemini_provider.py`.

1. Добавить тесты result codes и гарантии отсутствия частичной мутации.
2. Реализовать validator через `client.aio.models.get()` с перебором ключей и
   классификацией supported/unsupported/unavailable.
3. Внедрять validator в repository, чтобы unit-тесты не использовали сеть.
4. Сначала сохранять override в БД, затем обновлять live settings.

## Этап 4. Рабочее удаление и понятный `/models`

**Файлы:** `app/handlers/cmd_models.py`, новый
`tests/test_cmd_models.py`, при необходимости `app/config.py`.

1. Добавить тесты четырёх провайдеров, source label, result-specific сообщений,
   короткого delete callback и обработки устаревшего token.
2. Перевести UI на catalog snapshots и короткие hash tokens.
3. После удаления сразу перерисовывать список.
4. Добавить FreeTheAI в provider selector.

## Этап 5. Selector, fallback и reload safety

**Файлы:** `app/providers/router.py`, `app/agent_use_cases.py`,
`app/handlers/menus.py`, `app/repos/chats.py` и соответствующие тесты.

1. Добавить падающие тесты прямой маршрутизации новой Gemini-модели,
   полностью пустого selector и безопасной миграции чатов.
2. Оставить hardcoded Gemini-модели только в fallback-порядке.
3. Не мигрировать чаты, если selectable destination отсутствует.
4. Не возвращать скрытые role-модели в `/model`.

## Этап 6. Документация и полная проверка

**Файлы:** `README.md`, `CHANGELOG.md`.

1. До и после документационных изменений запустить
   `python scripts/check_encoding.py`.
2. Документировать `none`, env/admin precedence, dynamic Gemini validation и
   удаление моделей.
3. Запустить targeted suites, затем полный `pytest`, Ruff для изменённых
   файлов, `git diff --check` и encoding verifier.
4. Сопоставить результаты с каждым требованием design spec и исходной цели.
