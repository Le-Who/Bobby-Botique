# ADR 0002: единая транзакционная запись графа памяти

- **Статус:** принято
- **Дата:** 2026-08-27
- **Область:** LTM, GraphRAG, provenance, RLS

## Контекст

Граф долгосрочной памяти пополняется двумя независимыми путями:

1. быстрая фоновая extraction после подходящего пользовательского сообщения;
2. периодическая consolidation накопленных воспоминаний.

Раньше оба пути самостоятельно разрешали сущности, обновляли temporal edges и
сохраняли provenance. Реализации постепенно разошлись: один путь мог открыть
вложенную транзакцию или получить новое соединение, другой — применить иной
порядок merge/upsert. Это создавало риск частичного коммита, потери связи с
исходной памятью и обхода RLS-контекста вызывающего workflow.

После нормализации provenance в `memory_edge_sources` такие расхождения стали
особенно опасны: граф без живого источника не должен участвовать в retrieval, а
удаление источника должно атомарно пересчитать или удалить производную связь.

## Решение

1. `app/repos/memory_graph_writer.py` является единственной общей границей
   записи `memory_nodes`, `memory_edges` и `memory_edge_sources` для extraction
   и consolidation.
2. Вызывающий workflow заранее выполняет внешние вызовы и строит immutable
   `GraphMutationPlan`. Внутри write-транзакции нет сетевых запросов и расчёта
   embeddings.
3. Вызывающий workflow владеет соединением, транзакцией, RLS user context,
   advisory lock и проверкой `memory_epoch`. Writer принимает готовый `conn` и
   никогда не обращается к глобальному pool.
4. Каждый node и edge candidate обязан иметь непустой набор положительных
   durable `source_memory_ids`. План без provenance отклоняется до SQL-записи.
5. Writer детерминированно:
   - разрешает exact и semantic-equivalent nodes;
   - объединяет дубликаты внутри плана;
   - закрывает только явно разрешённые temporal conflicts;
   - обновляет exact edges с монотонным `is_core` и максимальным весом;
   - записывает нормализованные provenance rows;
   - пересчитывает compatibility-массив `source_memory_ids` из живых источников.
6. Source memories/facts, graph mutations, provenance и source markers
   фиксируются одной caller-owned транзакцией. Любая ошибка откатывает весь
   workflow.

## Инварианты

- Графовая связь без durable provenance не создаётся.
- `user_id` применяется ко всем node, edge и provenance операциям.
- Writer не может случайно закоммитить раньше вызывающего workflow.
- Writer не меняет RLS-контекст и не открывает скрытое соединение.
- Semantic node resolution использует один общий threshold
  (`SEMANTIC_NODE_DISTANCE = 0.12`).
- Conflict closure выполняется только для edge ID и predicate, одобренных
  подготовительным этапом.
- Удаление или expiry источника остаётся совместимым с триггерами migration
  `067`, которые пересчитывают оставшуюся поддержку edge.

## Последствия

Положительные:

- extraction и consolidation больше не расходятся по правилам записи;
- provenance и tenant isolation становятся частью API writer, а не соглашением
  между несколькими SQL-копиями;
- транзакционные тесты могут доказать rollback всего graph workflow;
- новые источники LTM могут переиспользовать один mutation contract.

Цена решения:

- вызывающий код обязан подготовить embeddings и source IDs заранее;
- `GraphMutationPlan` требует явного преобразования provider output в
  нормализованные dataclass-кандидаты;
- изменения schema-level merge semantics нужно вносить централизованно и
  проверять одновременно для extraction и consolidation.

## Отклонённые альтернативы

- **Оставить две SQL-реализации и синхронизировать тестами.** Дублирование
  остаётся источником дрейфа, а тестовая матрица растёт квадратично.
- **Пусть writer сам получает соединение.** Нарушает атомарность с source
  memories и может потерять caller-bound RLS context.
- **Выполнять embeddings внутри транзакции.** Удерживает блокировки во время
  медленного сетевого вызова и повышает вероятность rollback/timeout.
- **Хранить provenance только в массиве edge.** Не даёт строгих composite FK,
  надёжного каскада и tenant-safe join к исходным воспоминаниям.

Полный план миграции и тестовая матрица зафиксированы в
`docs/superpowers/specs/2026-08-27-codebase-hardening-and-ltm-deepening-design.md`
и
`docs/superpowers/plans/2026-08-27-codebase-hardening-and-ltm-deepening.md`.
