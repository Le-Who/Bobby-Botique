# Исправление и расширение тестового покрытия — 2026-10-09

Статус: **выполнено и проверено**. Все этапы, итоговые измерения и
ограничения сохранены в
[итоговом отчёте](../../test-coverage-improvements-2026-10-09.md).

Цель: выполнить очередь из [карты покрытия](../../test-coverage-map-2026-10-09.md),
усилить существующие проверки, добавить недостающие сценарии и исправить
воспроизводимые дефекты, обнаруженные этими проверками. Исходный аудит остаётся
историческим измерением; результаты исправлений будут отдельным приложением.

Основание: 30 сценариев и 20 замечаний к качеству тестов в
[evidence.json](../../test-coverage/2026-10-09/evidence.json). Два замечания
описывают ограничения полезных тестов; их нельзя «исправлять» ослаблением oracle
или ожиданиями, вычисленными тем же кодом, который проверяется.

Архитектура: проверить наблюдаемые контракты существующих границ — Telegram
delivery, typed providers, callback/state ownership, memory persistence, web
workers и browser lifecycle. Для асинхронных гонок использовать управляемые
barriers и подтверждать порядок эффектов. PostgreSQL, Redis и observability
проверять настоящими изолированными сервисами; mock не заменяет этот слой.

Инструменты: Python 3.14, locked uv 0.12.6, pytest/asyncio/coverage, Node VM,
Playwright Chromium, asyncpg и redis. Зависимости и lockfile не обновлять.

## Общие ограничения

- Сохранить исходные локальные изменения документации; работать на текущей
  ветке `vps_testai`, без commit/push/PR/deploy.
- Не читать `.env`; не использовать production credentials, данные и сервисы.
- Для offline pytest использовать сохранённый runner с отдельным output directory
  на каждую задачу. Integration DDL/DML допускается только в созданной для теста
  изолированной базе. Пропущенный тест не считается выполненной проверкой.
- Реальный дефект сначала воспроизвести падающим тестом, затем исправить и
  проверить соседние контракты. Новый тест уже корректного поведения должен
  иметь независимый oracle; не придумывать искусственный RED.
- Сабагенты работают параллельно только с непересекающимися владельцами файлов.
  Контроллер объединяет результаты и выполняет независимое review.
- Не менять продуктовый контракт ради процента покрытия. Сохранить reconnect
  Daily 2048, public publication gate, consent epochs и caller-owned transactions.
- Документировать фактические проверки и ограничения; не заявлять live Telegram,
  provider, VPS или acoustic validation по локальным тестам.

## Очередь исполнения

- [x] 01. Усилить/переименовать 18 слабых проверок: CORE-Q01–Q07, SQ-01–SQ-04,
  quality-product-01–06 и ROOT-Q01. Сохранить полезные quality-product-07/08.
- [x] 02. Проверить реальную private delivery, частичную Telegram публикацию и
  subprocess cleanup: CORE-G01, CORE-G02, CORE-G06.
- [x] 03. Проверить destructive callbacks, board identity, voice ownership и
  natal access revocation: product-02, product-03, product-06, product-07.
- [x] 04. Исправить browser CI discovery; проверить AudioWorklet sample count,
  mic lifecycle, Mini App memory delete и Reader UI: ROOT-01–ROOT-03.
- [x] 05. Покрыть malformed routed streams, album handling, model classification
  и native adapter contracts: CORE-G05, CORE-G07–CORE-G09.
- [x] 06. Проверить compaction/task drain, debounce ownership, UserState eviction
  и реальную Redis LLM semaphore: ST-04–ST-07.
- [x] 07. Проверить ошибочные cleanup/lifecycle ветви: CORE-G03.
- [x] 08. Покрыть реальные PostgreSQL CAS/consent/provenance/RLS, legacy upgrades,
  concurrent trivia и natal persistence: CORE-G04, ST-01–ST-03, ST-08, ST-09,
  product-04. Проверить актуальное назначение pgvector smoke.
- [x] 09. Проверить реальные Redis preparation/Crocodile leases: product-08.
- [x] 10. Проверить horoscope partial delivery/retry/claims: product-05.
- [x] 11. Проверить ranked Daily 2048 integrity с сохранением legitimate recovery:
  product-01 и quality-product-07.
- [x] 12. Проверить изолированную полную observability ingestion: ROOT-04.
- [x] 13. Независимо пересмотреть изменения, выполнить соответствующие gates,
  повторить полное измерение и обновить документ выполнения с точными nodeids.

## Review и критерии завершения

Для каждой задачи сверить acceptance исходного finding, диагностическую силу
assertions и отсутствие побочных изменений. Для новых багов сохранить evidence
RED → GREEN; для гонок проверить контролируемое межплетение, cancellation и
ownership. Завершение требует явного статуса всех 30 сценариев и 20 замечаний,
доступных offline/service/browser проверок, Ruff, format, mypy, encoding и
`git diff --check`. Не выполненный реальный service-layer сценарий остаётся
открытым, даже если тест для него написан.
