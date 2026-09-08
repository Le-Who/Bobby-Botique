# Domain Context

Проверено по checkout `8fc19516` на 2026-09-08. Это словарь текущих контрактов;
исторические планы не определяют состояние работающего deployment.

## AI response delivery

### Streaming Response

Ответ AI, который поступает как последовательность text deltas и завершается
ровно одним terminal Generation Event.

### Generation Event

Типизированное событие provider stream: text delta, completion, failure или
deferred generation. Finish reason, usage, grounding и route являются данными
terminal event, а не скрытым глобальным состоянием или пользовательским текстом.

### Response Delivery

Полный путь от Streaming Response или готового ответа до показанного Telegram
response. Response Delivery единолично владеет финальным текстом и Action
Keyboard, включая formatting, splitting, Long Read и fallback.

### Presentation

Чистое преобразование canonical AI content и typed final status в отображаемый
текст и Telegram Action Keyboard. Presentation не выполняет Telegram edits.

### Action Keyboard

Полный набор Telegram inline actions финального response. Publication row для
Reader или Telegraph добавляется первой; base rows сохраняют исходный порядок.
После завершения Response Delivery handlers не изменяют Action Keyboard.

### Long Read

Режим Response Delivery для ответа, formatted HTML которого не помещается в
один Telegram message. Цепочка: Reader → разрешённый Telegraph → Telegram split.
При `TELEGRAPH_PUBLICATION_ENABLED=false` (default) публичная публикация пропускается.

### Reader

Mini App, читающий полный Long Read из Redis. Reader URL считается пригодным
для delivery только после подтверждённой записи content и успешного отображения
Reader action пользователю.

### Telegraph Fallback

Публичная публикация Long Read в Telegraph только при явном
`TELEGRAPH_PUBLICATION_ENABLED=true`. При недоступности Reader она создаётся
синхронно; после успешного Reader delivery может создаваться в фоне как cold
storage. Ни приватность ссылки, ни вечная доступность стороннего сервиса не
гарантируются; без opt-in оба пути отключены.

### Delivery Outcome

Типизированный результат Response Delivery: complete, partial, failed или
deferred. Он отделяет canonical content от displayed text и содержит immutable
Telegram Message Reference вместо mutable Telegram Message.

### Telegram Message Reference

Идентификаторы chat, message и optional thread, необходимые downstream
операциям вроде TTS. Reference не разрешает handler повторно редактировать
финальный response.

## State и долгосрочная память

### UserState

Process-local LRU-состояние с локальным `asyncio.Lock` и отложенной записью
persisted fields в PostgreSQL. Не является Redis-распределённым объектом.

### Private Memory Consent

Durable разрешение на приватную LTM-работу, связанное с уникальным `memory_epoch`.
Provider leases удерживают актуальную эпоху во время внешних операций; disable
или erasure отзывает старую эпоху. Групповые сообщения не включаются в приватную
память неявно.

### Durable Provenance

Живые исходные `long_term_memory` rows, связанные с graph edges через
tenant-scoped `memory_edge_sources`. Массив `source_memory_ids` — compatibility
snapshot, а не самостоятельная замена нормализованному происхождению.

### Graph Mutation Plan

Immutable набор кандидатов nodes/edges и разрешённых temporal closures,
подготовленный до write-транзакции. Общий writer использует соединение,
транзакцию и tenant context вызывающего workflow; не выполняет provider calls.

## Модели и эксплуатационные свидетельства

### Role Model и Selectable Model

Внутренняя модель роли и элемент пользовательского каталога — разные понятия.
Env задаёт baseline; явный admin override имеет приоритет. `none` означает пустой
каталог, а не запрос автоматически добавить скрытые defaults.

### Verification Evidence

Результат конкретной команды для конкретного checkout/окружения. Старый pass count,
статус плана или успешная сборка не доказывают текущую работу VPS и внешних API.
