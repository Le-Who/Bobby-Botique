# План реализации безопасного LTM

## Этап 1. Baseline и schema contract

1. Добавить RED tests для BIGINT graph IDs, `memory_nodes.updated_at`, RLS,
   provenance и `memory_epoch`.
2. Выпустить migration `067`, не изменяя применённые migrations.
3. Исправить runtime casts и graph fallback, проверить tenant-bound passages.

## Этап 2. Consent, epoch и фоновые задачи

1. Тестами зафиксировать отсутствие text/document/group writes при LTM off.
2. Добавить repository consent+epoch boundary и атомарный insert/prune.
3. Передавать epoch во все автоматические capture paths.
4. Инвалидировать epoch при off/clear, регистрировать и отменять user tasks.

## Этап 3. Provenance, deletion и TTL

1. Писать нормализованные edge sources при extraction/consolidation.
2. Объединить manual/full/expiry/cap cleanup semantics.
3. Фильтровать retrieval по живой provenance и expiry.
4. Подключить bounded scheduled cleanup.

## Этап 4. Безопасная consolidation

1. RED tests: all/partial embedding failures и concurrent snapshot.
2. Перевести consolidation на non-destructive `consolidated_at`.
3. Готовить replacements вне transaction, затем lock/recheck/commit.
4. Сохранять conservative expiry и фактический inserted count.

## Этап 5. Prompt/history/privacy hardening

1. Разделить provider-ready и canonical retained history.
2. Сделать chunking lossless при `MAX_CHUNKS`.
3. XML-escape memory layers и добавить untrusted-data directive.
4. Ограничить memory UI private/owner scope и проверить Mini App `auth_date`.

## Этап 6. PTB ConversationHandler

1. Добавить invariant tests для четырёх hybrid wizard handlers.
2. Ввести общую фабрику с точным локальным suppression известного warning.
3. Убедиться, что полный suite больше не содержит 10 косметических warnings.

## Этап 7. Completion audit

1. Запустить targeted suites после каждого RED → GREEN цикла.
2. Запустить полный `pytest`, Ruff, `git diff --check` и encoding verifier.
3. Проверить diff на секреты, unrelated changes и migration rollback risks.
4. Закоммитить логическими commits и отправить `vps_testai` в upstream.
