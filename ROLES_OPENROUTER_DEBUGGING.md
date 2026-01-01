# Отладка проблемы с применением ролей в OpenRouter

## Проблема

Роли не применяются для моделей OpenRouter, хотя работают корректно для Gemini.

## Выполненные исправления

### 1. Исправлена двойная композиция системного промпта

**Проблема:** `compose_system_instruction` вызывалась дважды - при сохранении роли и при использовании.

**Решение:** Теперь при сохранении роли сохраняется только промпт роли, а `compose_system_instruction` вызывается один раз при использовании.

**Измененные файлы:**
- `app/handlers/callbacks.py` - исправлено сохранение ролей

### 2. Добавлено детальное логирование

**Цель:** Понять, что происходит с `system_instruction` при передаче в OpenRouter.

**Добавлено логирование в:**
- `app/handlers/agent.py` - логирование перед вызовом `_get_ai_response`
- `app/services.py` - логирование в `get_openrouter_response` и `_execute_openrouter_request`
- `app/services.py` - логирование при добавлении системного сообщения в messages

**Что логируется:**
- Наличие и длина `system_instruction` на каждом этапе
- Наличие системного сообщения в массиве `messages`
- Предпросмотр содержимого системного промпта

### 3. Добавлен system_instruction во все вызовы _get_ai_response

**Проблема:** В некоторых функциях (`_handle_qna_search`, `_handle_research_agent`) не передавался `system_instruction`.

**Решение:** Добавлена передача `system_instruction` во все вызовы `_get_ai_response`.

**Измененные функции:**
- `_handle_qna_search` - добавлен `system_instruction`
- `_handle_research_agent` - добавлен `system_instruction`

## Что нужно проверить

### 1. Проверить логи

После применения роли и запроса к OpenRouter, в логах должно быть видно:

```
🔍 _get_ai_response: routing to OpenRouter, model=..., system_instruction=provided, length=...
🔍 get_openrouter_response called: model=..., system_instruction=provided, length=...
✅ OpenRouter: Added system instruction (length: ..., preview: ...)
✅ OpenRouter: Request includes system message (length: ..., first 200 chars: ...)
📤 OpenRouter: Sending request with X messages, model: ...
```

Если видно предупреждения:
```
⚠️ OpenRouter: system_instruction is None or falsy
⚠️ OpenRouter: Request does NOT include system message!
```

Это означает, что `system_instruction` не передается или пустой.

### 2. Проверить формат роли в БД

Если роль была применена **ДО исправления**, она может содержать дублированный базовый промпт в БД.

**Решение:** Переприменить роль после исправления кода.

### 3. Проверить правильность формата

Системное сообщение в OpenRouter должно быть:
```python
messages = [
    {
        "role": "system",
        "content": "<полный системный промпт>"
    },
    {
        "role": "user",
        "content": "<вопрос пользователя>"
    }
]
```

Это проверяется логированием.

## Следующие шаги

1. Перезапустить бота, чтобы применить изменения
2. Переприменить роль (если она была применена до исправления)
3. Сделать запрос к OpenRouter модели
4. Проверить логи и найти, где теряется `system_instruction`
5. Если проблема сохраняется, проверить формат данных в БД

## Известные проблемы

1. **Старые роли в БД** - если роль была применена до исправления, она может содержать дублированный базовый промпт. Нужно переприменить роль.

2. **Логирование может не выводиться** - если уровень логирования настроен на WARNING или выше, INFO логи не будут видны. Нужно убедиться, что уровень логирования позволяет видеть INFO.

