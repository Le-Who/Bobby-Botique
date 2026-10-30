# Request Processing and Dashboards Implementation Plan

> **Implementation record:** Subsequent user authorization started implementation of R1–R4, D1–D7 and O1–O5. See the [dated implementation evidence](../../request-processing-dashboard-implementation-2026-10-28.md) for delivered behavior and verification. The original checklist below is retained as design history, not a fresh execution authorization or current operational runbook.

> **For agentic workers:** Use the `executing-plans` skill to implement the selected plan task by task. If execution is explicitly delegated, use `subagent-driven-development`. Steps use checkbox syntax. This document is a handoff proposal; it does not authorize implementation, commits, publication or production operations by itself.

**Goal:** Устранить подтверждённые ошибки обработки/очередей и Daily, затем дать администратору достоверную диагностику и безопасное управление.

**Architecture:** Сохранить PTB, существующий provider router, single-owner delivery, runtime settings и Redis queue. Исправлять ownership и доменные ограничения у их текущих владельцев; выносить только новые самостоятельные протоколы в небольшие модули. Общую наблюдаемость строить поверх существующих событий, не вводить второй журнал приватных данных.

**Tech Stack:** Python `>=3.14,<3.15`, uv `0.13.5`, python-telegram-bot из lockfile, Quart, asyncio, Redis/Lua, PostgreSQL/asyncpg, существующий vanilla JS/CSS, Node 24 и locked Playwright.

**Spec:** [Аудит от 2026-10-28](../../request-processing-dashboard-audit-2026-10-28.md). Номера R/Q/D/O/U/I в нём — findings; R1/D1/O1 ниже — задачи реализации.

## Global Constraints

- Прочитать актуальные `AGENTS.md`, все соответствующие разделы `CODING_STANDARDS.md`, `CONTRIBUTING.md`, `docs/README.md` и действующие ADR перед своей частью.
- Сначала `git status --short`. При аудите были незакоммиченные router/provider/inline/thinking изменения и тесты; не сбрасывать и не присваивать их. При изменении базы заново воспроизвести finding.
- Это документационный handoff. Выполнять код только по следующему заданию пользователя; commits, pushes, PR, deploy, production migrations требуют соответствующего requested scope.
- Не читать/печатать `.env`, полные ключи, birth data, дампы и приватные логи. В тестах только синтетические пользователи и сервисы.
- Не менять зависимости без отдельной технической необходимости; использовать locked команды. Не добавлять frontend framework или отдельный брокер.
- Сохранить ADR single-owner delivery, privacy/account erasure, явные provider boundaries, per-operation settings snapshot, opt-in Telegraph и явное thinking пользователя.
- Redis job metadata переживают рестарт процесса при сохранном Redis; не обещать сохранность при потере Redis или exactly-once Telegram delivery.
- Все новые policy defaults ниже — предлагаемая начальная конфигурация, а не измеренные production SLO. Проверить нагрузочным стендом до production rollout.
- Миграции additive, идемпотентные по правилам репозитория, с текущим RLS/privileges. Номера `075`–`078` ниже свободны в снимке аудита; если заняты к реализации, выбрать следующий по manifest и исправить ссылки плана. Не менять уже применённые миграции.
- Читать live данные для измерений или выполнять внешний cutover только в явно запрошенном объёме. Локальная реализация плана не означает разрешение на production.

## Review Focus

1. Отмена до запуска coroutine и после взятия ресурса: все admission slots/leases освобождены ровно один раз — R1/R2/R4b.
2. Redis принял запись, но ответ потерян; Telegram принял отправку, но ответ неизвестен: разные сценарии, ни один не решать слепым повтором — R4a/D7.
3. Первый игрок и администратор меняют одну дату одновременно: общий DB lock, ни одной партии со смешанными правилами — D1/D2.
4. Две вкладки, несохранённая соседняя форма и устаревший HTTP-ответ: данные и черновики сохраняются — D3/D5/O4.
5. Нет Redis/DB, перезапуск процесса и недоступен один источник: degraded/unknown видимы, не превращаются в нулевые метрики или ложный успех — R4/O2/O3.

## 1. Разделение на исполнимые части

| План | Задачи | Результат |
| --- | --- | --- |
| [Обработка и очереди](2026-10-28-request-runtime.md) | R1, R2, R3, R4a–R4d | Ограниченный ingress; корректные leases/deadlines; очередь без подтверждённого дубля, с точными исходами и ограничениями |
| [Целостность Daily](2026-10-28-daily-integrity.md) | D1–D7 | Стабильные правила игр, fenced preparation, правильные кнопки, сохраняемые drafts, управляемые jobs и delivery journal |
| [Панели и диагностика](2026-10-28-operator-dashboards.md) | O1–O5 | Метрики этапов и всех очередей, дешёвый snapshot, осторожные controls, доступные формы и corpus intent |

Каждая задача содержит файлы, интерфейс, regression oracle и команду проверки. Новые имена файлов/API обозначают предлагаемый контракт, а не уже существующую реализацию. Изменять контракт можно при обнаруженном конфликте с актуальным кодом, с обновлением зависимых задач и доказательством эквивалентных гарантий.

## 2. Рекомендуемая последовательность

1. **Волна A — целостность:** R1 → R2 → R3; отдельно D1 → D2 и безопасный DOM из D5. До этой волны не добавлять кнопки cancel/retry очередей.
2. **Волна B — надёжные операции:** R4a → R4b → R4c → R4d; D3 → D4 → оставшаяся D5. O1 можно выполнять после появления правильных owner events.
3. **Волна C — Daily jobs и журнал:** D6 после R4 и D1–D4; D7 после D6. Массовая отправка остаётся отключённой в тестах, кроме фальшивого транспорта.
4. **Волна D — панели:** O2 → O3, O4 после нового job protocol, O5 независимо от Redis/DB миграций. Сначала read-only очередь, затем actions с проверенными переходами состояний.

Не запускать параллельное редактирование `app/web.py`, `admin_daily.html` или `queue.py` без заранее закреплённых границ. Независимость бизнес-тем не означает отсутствие файловых конфликтов.

## 3. Рабочий цикл каждой задачи

- [ ] Сверить исходный finding с текущим кодом и отметить изменившиеся условия.
- [ ] Добавить указанный regression test; выполнить его и получить отказ по целевому поведению, а не по сломанному fixture/import.
- [ ] Исправить минимальный слой-владелец. Не переписывать соседние подсистемы.
- [ ] Выполнить focused tests задачи. Для SQL/Lua отдельно пройти изолированные service contracts по `CONTRIBUTING.md`; mocks недостаточны для race/atomicity.
- [ ] Обновить соответствующие текущие guides, event catalog и env registry только если контракт действительно изменился.
- [ ] Проверить diff, отсутствие чувствительных данных и сохранить короткое evidence: команда, результат, границы. Только после этого переходить к зависимой задаче.

## 4. Финальные gates реализации

После последней затронутой задачи один раз выполнить соответствующие locked lint/type/format checks из `CONTRIBUTING.md`, полный offline suite и browser suite; повторять только при новом исправлении или неразрешённом отказе. Integration suite — в предусмотренном изолированном Redis/Postgres окружении, без перенаправления на production.

```powershell
uv run --locked pytest tests/ --ignore=tests/integration -m "not integration and not browser" --override-ini="addopts=" --timeout=30
$env:GEMAIBOT_REQUIRE_BROWSER_TESTS = "1"
uv run --locked pytest tests/ -m browser -n 0 --override-ini="addopts=" --timeout=30
python -X utf8 scripts/check_encoding.py
python -X utf8 scripts/check_docs_links.py
python -X utf8 scripts/check_env_registry.py
git diff --check
```

Дополнительные обязательные итоговые сценарии:

- 10 000 синтетических updates и медленный hot user: число accepted outstanding не превышает лимит, нет утечки coroutine/lock/task, другой уже принятый пользователь и inline обслуживаются.
- Redis interruption/recovery, потеря lease, timeout после commit: ограничены попытки/байты/возраст, нет локальной второй копии, stale owner не меняет состояние.
- Конкурентный старт Daily и edit/reset: стабилен один опубликованный набор правил; stale preparation не пишет assets; результаты не удаляются.
- Две админские вкладки и reload во время job: сохраняются draft и состояние job; HTTP timeout не создаёт второй запуск/рассылку.
- 20 вкладок monitoring: число expensive source calls ограничено TTL, не числом вкладок; источник с ошибкой помечен отдельно.

Нагрузочный тест с fake providers доказывает границы и накладные расходы, но не качество/скорость внешних моделей. Сравнение live TTFT, p95, rate-limit и фактических расходов — отдельный разрешённый canary после локальной готовности.

## 5. Cutover и rollback для будущего выпуска

План не запускает эти операции. Перед разрешённым выпуском подготовить конкретный checklist:

1. Additive DB migration, совместимость чтения старой/новой версии, backup/rollback согласно штатной процедуре.
2. Старую очередь остановить на admission, дождаться drain либо выполнить управляемое восстановление по действующему [runtime-controls](../../runtime-controls.md). Не запускать старые и новые Lua consumers на несовместимом payload.
3. Сверить агрегаты queued/running/terminal, без дампа тел заданий. Переключить workers; старые malformed записи сохранить для отдельной обработки.
4. При откате остановить admission/дождаться owners, затем вернуть совместимую версию. Не удалять новые results/journal/revision columns и не откатывать данные destructive SQL.
5. Критерий остановки canary: дубль delivery, запись stale generation, потеря draft, превышение лимита outstanding, расходящиеся terminal counters. Не компенсировать это увеличением concurrency.

## 6. Краткое задание для следующего агента

> Прочитай аудит `docs/request-processing-dashboard-audit-2026-10-28.md` и этот master plan. Реализуй выбранную волну по связанным планам, начиная с regression tests. Сохрани текущие незакоммиченные изменения. Не выполняй commits/push/deploy/migrations на production без соответствующего задания. Для каждой законченной задачи сообщи: исправленное поведение, тестовый результат и оставшиеся границы. Не объявляй mocks доказательством реальной Redis/DB atomicity или production latency.
