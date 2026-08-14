# ADR 0001: единый владелец доставки AI-ответа

- **Статус:** принято
- **Дата:** 2026-08-14
- **Область:** provider streaming, Telegram delivery, Long Read

## Контекст

Длинный ответ успешно публиковался в Telegraph, но пользователь не получал
ссылку. Streaming-код добавлял publication row, после чего handler заменял всю
клавиатуру своими action rows через `edit_reply_markup()`.

Это был не изолированный дефект кнопки. Финальное Telegram-сообщение имело
несколько владельцев, а provider metadata передавалась скрыто через
`ContextVar`, tuple positions и пользовательский error text. Long Read также
имел две реализации с разными fallback semantics. Любой новый post-processing
шаг мог потерять текст, metadata или клавиатуру без нарушения интерфейса.

## Решение

1. `ProviderRouter.stream()` принимает immutable `GenerationRequest` и выдаёт
   zero or more `TextDelta`, затем ровно один terminal event:
   `StreamCompleted`, `StreamFailed` или `StreamDeferred`.
2. Finish reason, token usage, grounding, фактический route и failure phase
   передаются только typed values. Provider layer не зависит от Telegram.
3. `TelegramResponseDelivery` является единственной handler-facing точкой для
   streamed и already-completed AI-ответов.
4. `AIStreamCoordinator` владеет request lifecycle, а
   `TelegramRenderSession` — progressive draft и единственной финализацией.
5. Renderer единолично формирует финальный text и всю keyboard. Publication row
   всегда добавляется первой к копии domain actions.
6. Long Read имеет один fallback chain: Redis Reader → Telegraph → Telegram
   split. Успех публикации считается delivery только после успешного показа
   ссылки пользователю.
7. Handler получает immutable `TelegramResponseOutcome`/`DeliveryReceipt` и не
   редактирует финальную клавиатуру после delivery.
8. Старые `stream_response`, `stream_and_display`, `StreamingWriter`,
   `StreamingUIAdapter`, metadata `ContextVar` и error-as-text path удаляются в
   этой же миграции; постоянный compatibility façade не создаётся.

## Инварианты

- В provider stream ровно один terminal event, после него нет событий.
- Race выигрывает первый непустой текст; metadata проигравшего не протекает.
- После первого текста key/model route не переключается.
- Cancellation всегда закрывает и ожидает request-scoped tasks.
- Canonical content отделён от displayed text, notices и footer.
- Telegram limit проверяется после formatting/sanitization.
- Reader/Telegraph row не заменяет normal или recovery actions.
- Finalization выполняется ровно один раз.

## Последствия

Положительные:

- исходный класс дефектов с потерей ссылки или action rows становится
  структурно невозможным;
- provider routing можно тестировать без Telegram, а presentation — без сети;
- failures, deferred state и partial completion больше не маскируются текстом;
- chat, QnA, photo, document, inline, deferred и agentic delivery используют
  одинаковые правила там, где их поведение должно совпадать.

Цена решения:

- migration затрагивает все direct stream consumers одновременно;
- новые provider и delivery events требуют явной обработки при расширении;
- rollback/history/TTS logic должна зависеть от typed outcome, а не от текста.

## Отклонённые альтернативы

- **Локально сохранять Reader row в каждом handler.** Оставляет несколько
  владельцев keyboard и требует помнить неявный ordering contract.
- **Оставить старый API как façade.** Сохраняет tuple/error-text semantics и
  создаёт два источника истины на неопределённый срок.
- **Передавать metadata через `ContextVar`.** Metadata гоняющихся задач может
  относиться не к победившему stream и не проверяется типами.
- **Всегда использовать только Telegraph.** Удаляет существующий Reader UX и
  не решает атомарность показа ссылки.

Полная совместимость и тестовая матрица зафиксированы в
`docs/superpowers/specs/2026-08-14-ai-response-delivery-architecture-design.md`.
