# Актуализация документации — 2026-09-20

## Область и границы

Проверенный checkout: **`f44541a7`**. Сверены текущие справочники в корне,
`docs/` и `ops/observability/` с manifest/lock, конфигурацией, владельцами
runtime-сценариев, тестами и CI/deploy. Особое внимание уделено изменениям
после предыдущего аудита: логирование, log viewer, Daily Crocodile, webhook
и исправления доступности интерфейса.

Изменены только Markdown-файлы. Runtime, зависимости, SQL и workflows не
менялись. `.env`, реальные сообщения, production DB и приватные логи не читались;
миграции, live provider calls, Docker/VPS проверки, commit/push/deploy не выполнялись.
Наличие теста указано как источник контракта, а не как его успешный прогон сегодня.

Это сверка документации с реализацией и основание для roadmap, а не полный
security audit или доказательство корректности каждого участка приложения.
Внешние цены, provider entitlements, состояние GitHub queue и deployment не
проверялись. Старые test/pass/performance/deploy сведения сохранены как история.

## Исправленные расхождения

| Тема | Фактическая реализация и изменение документации |
| --- | --- |
| Агентные инструкции | `AGENTS.md` актуализирован по коду и официальным рекомендациям Astra: релевантное чтение, работа в разрешённом scope, отсутствие повторных согласований рутинных действий, соразмерные проверки и явные технические границы. `GEMINI.md` остаётся указателем |
| Webhook configuration | Добавлены `WEBHOOK_SECRET_TOKEN`, `WEBHOOK_MAX_CONNECTIONS`, `UPDATE_QUEUE_MAXSIZE`; формат, comparison и clamps сверены с `app/webhook_security.py`, `app/config.py`, `bot.py` |
| Неполные model roles | README дополнен `INLINE_MODEL`, `OPENCODE_URL_SELECTION_MODEL`, `ELEVENLABS_MODEL` из effective loader |
| Env не равен deployment | Описаны переменные, читаемые кодом, но не передаваемые VPS workflow; отдельно — forwarded legacy names без загрузки через `load_settings()` |
| ElevenLabs | Вместо обещания round-robin описан последовательный перебор ключей с пропуском failed/exhausted внутри synthesis; недоказанная внешняя квота удалена. Источники: `app/providers/elevenlabs_tts.py`, `app/voice_engine.py` |
| Research budget | `AGENTIC_MAX_PAGES` ограничивает admitted calls за agent run, а не за итерацию. Time/token cutoffs проверяются между итерациями; текущий call и финальный synthesis могут превысить порог. Handler fallback создаёт новый run. Источники: `app/core/agentic.py`, `app/handlers/ai_search.py` |
| Research ownership/model | Уточнён direct Gemini SDK boundary и precedence `model_override` → `chat_state.model` → `AGENTIC_MODEL` → `RESEARCH_MODEL`. Provider research-role settings не переключают этот цикл на другой provider |
| Thinking | Вместо фиксированного числа heuristics описана фактическая последовательность explicit override → model default → classifier (`app/thinking_classifier.py`) |
| Health и metrics | `/health` использует DB connection flag; Redis не определяет общий status, providers не проверяются. `/metrics` unauthenticated/rate-limited, `/api/events` authenticated (`app/web.py`) |
| Log viewer deployment | Runbook отражает recreate/wait, running/non-restarting checks, Grafana health/auth, Alloy/Loki readiness и синхронизацию пароля Grafana с secret file (`.github/workflows/deploy.yml`) |
| Log viewer verification | Service readiness/login отделены от end-to-end ingestion; CI/deploy не утверждают, что новое synthetic bot event найдено в Loki |
| Secret-file permissions | Manual runbook уточняет host ownership и доступ container group к секрету. Compose start сам по себе не меняет пароль в существующей Grafana DB |
| Event retention | Каталог событий больше не называет collector будущим: стек уже реализован. Указаны Loki retention/asynchronous deletion и отсутствие host-loss backup |
| Daily Crocodile | Главные справочники теперь отражают per-process models, image quota, readiness и preparation; детальные правила остаются в профильном runbook |
| Исторические планы | Планы logging overhaul и log viewer получили явный архивный статус; индекс связывает их с текущими владельцами |
| Roadmap | Короткий список заменён категориями с текущей базой, инициативами, P0/P1/P2, оценкой объёма, зависимостями, приёмкой и порядком поставки |

## Матрица проверенных источников

| Область | Основные источники | Результат |
| --- | --- | --- |
| Runtime/tooling/setup | `pyproject.toml`, `uv.lock`, `Dockerfile`, `pytest.ini`, `.pre-commit-config.yaml`, CI | Python 3.14, uv 0.12.6 и Ruff 0.15.2 остаются актуальны; dependency graph не изменён |
| Configuration/commands | `app/config.py`, `app/bot_commands.py`, `app/repos/models_repo.py`, deploy | Сохранены role/catalog и env/admin override границы; нет обещания provider availability |
| Delivery/state/LTM | `app/response_delivery/`, `app/state.py`, `app/repos/memory_consent.py`, `app/repos/memory_graph_writer.py`, ADR 0001/0002 | Single-owner delivery, process-local state и caller-owned graph transaction остаются действующими границами |
| Natal | `app/natal/`, manifest, CLI scripts, release fixture и deploy gate | PyEphem/local equal-house и local city path остаются актуальны; старые точность/timing/VPS результаты не переаттестованы |
| Observability | `app/observability/`, `app/utils/logging_config.py`, `ops/observability/`, CI/deploy | Подтверждены bounded content modes, event owners и stack gates; live capacity неизвестна |
| Daily products | `app/games/daily_preparation.py`, `crocodile_daily.py`, admin UI/API, quota/preparation tests | Readiness не вызывает AI; admin preparation может вызывать providers и обходить automatic quota |
| Web/UX | `app/web.py`, `app/webhook_security.py`, `bot.py`, templates и accessibility tests | Уточнены auth/health границы; HTML tests не приравнены к проверке Telegram на устройствах |

Файлы без смысловых расхождений не переписывались ради новой даты.
В частности, ADR rationale, `GEMINI.md`, dependency-decision и natal historical
evidence сохраняют исходную датировку. [Предыдущий аудит](documentation-audit-2026-09-08.md)
остаётся отдельным историческим документом.

## Подтверждённые ограничения для roadmap

1. Нет общего hard deadline/token budget между research iterations, synthesis
   и fallback runs — выделено в **AI-3**, без изменения поведения в этой задаче.
2. `USE_OPENROUTER` и `MAX_CONCURRENT_HEAVY_CALLBACKS` forwarded в deployment,
   но не загружаются effective loader. Callback handler использует fallback 4.
   `ENABLE_PERSISTENT_QUEUE` присутствует в legacy Compose, но чтение этого
   имени не найдено в `app/`/`bot.py`. Решение об удалении или реализации — **DX-1**.
3. Части env-контракта не forwarded через VPS: например `INLINE_MODEL`,
   `OPENCODE_URL_SELECTION_MODEL`, `ELEVENLABS_MODEL`, `WEBHOOK_MAX_CONNECTIONS`,
   `UPDATE_QUEUE_MAXSIZE`. Поддержка переменной кодом не делает её рабочим Secret.
4. Log stack readiness не доказывает ingestion; текущие helper scripts позволяют
   подготовить следующий synthetic gate — **OBS-1**.
5. Single-DSN RLS и process-local state ограничивают hard isolation и несколько
   активных реплик — **SEC-2/SCALE-1**, только при соответствующей потребности.
6. Продуктовые quality/cost/load baseline, restore evidence и mobile/live проверки
   требуют отдельного измерения — **AI-1/REL-2/PERF-1/UX-1/MEDIA-1**.

## AGENTS.md и официальные рекомендации

20 сентября открыты официальные [рекомендации GPT-6 Astra](https://developers.openai.com/api/docs/guides/latest-model),
[статья о skills и AGENTS.md](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra)
и [правила discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md).
Применены рекомендации по релевантному контексту, устранению конфликтующих
процедур, автономности в заданных границах и пропорциональной проверке.
Настройки моделей самого бота не менялись; machine/plugin skills не редактировались.

## Проверка изменений

- `python -X utf8 scripts/check_encoding.py`: успешно до правок и после;
  заключительная проверка охватила 56 Markdown-файлов, включая неигнорируемые локальные.
- Статическая AST/TOML-сверка без импорта приложения: Python range, uv/Ruff pins
  и 37 документированных прямых env-defaults совпадают с исходниками. Численно
  эквивалентные `60`/`60.0` учтены; это не доказательство работы settings на VPS.
- Проверка 47 документов в корне, `docs/` и `ops/`: 176 локальных Markdown-ссылок,
  включая 4 heading anchors, разрешаются. Внешние ссылки массово не проверялись;
  официальные OpenAI-источники для `AGENTS.md` открыты отдельно.
- `git diff --check`: успешно. Изменения ограничены Markdown; fixtures, исходники,
  manifest/lock, SQL и workflows не изменены.

Полный runtime suite, integration DB и deployment не запускались: они не
подтверждают правильность prose-only изменений и требуют другого scope.
