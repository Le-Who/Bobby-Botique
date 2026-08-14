# Domain Context

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
один Telegram message. Long Read использует фиксированную цепочку Reader →
Telegraph → Telegram split.

### Reader

Mini App, читающий полный Long Read из Redis. Reader URL считается пригодным
для delivery только после подтверждённой записи content и успешного отображения
Reader action пользователю.

### Telegraph Fallback

Постоянная публикация Long Read в Telegraph. При недоступности Reader она
создаётся синхронно; после успешного Reader delivery создаётся в фоне как cold
storage и сохраняется для открытия после истечения Redis content.

### Delivery Outcome

Типизированный результат Response Delivery: complete, partial, failed или
deferred. Он отделяет canonical content от displayed text и содержит immutable
Telegram Message Reference вместо mutable Telegram Message.

### Telegram Message Reference

Идентификаторы chat, message и optional thread, необходимые downstream
операциям вроде TTS. Reference не разрешает handler повторно редактировать
финальный response.
