# Исправление ошибки Button_data_invalid в команде /model

## Проблема

Команда `/model` вызывала ошибку `Button_data_invalid` из-за того, что `callback_data` для моделей OpenRouter с длинными именами (например, `cognitivecomputations/dolphin-mistral-24b-venice-edition:free`) превышал лимит Telegram в 64 байта.

## Решение

Вместо передачи полного имени модели в `callback_data` используется индекс модели в списке. Это гарантирует, что `callback_data` всегда будет коротким (например, `model:0`, `model:1`, и т.д.).

## Изменения

### 1. `app/handlers/commands.py` - `model_command()`

**Было:**
```python
keyboard.append([InlineKeyboardButton(f"{is_selected}🤖 {m}", callback_data=f"model_{m}")])
```

**Стало:**
```python
keyboard.append([InlineKeyboardButton(f"{is_selected}🤖 {m}", callback_data=f"model:{model_index}")])
```

**Логика:**
- Создается единый список всех моделей (Gemini + OpenRouter)
- Для каждой модели используется индекс вместо полного имени
- Индексы начинаются с 0 и увеличиваются последовательно

### 2. `app/handlers/callbacks.py` - `model_button_callback()`

**Было:**
```python
model_name = query.data.split("_", 1)[1]
```

**Стало:**
```python
if query.data.startswith("model:"):
    # Новый формат: model:0, model:1, и т.д.
    model_index = int(query.data.split(":")[1])
    # Пересоздаем список моделей в том же порядке
    all_models = []
    if settings.AVAILABLE_MODELS:
        all_models.extend(settings.AVAILABLE_MODELS)
    if openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
        all_models.extend(settings.OPENROUTER_AVAILABLE_MODELS)
    model_name = all_models[model_index]
else:
    # Старый формат для совместимости
    model_name = query.data.split("_", 1)[1]
```

**Особенности:**
- Поддержка нового формата `model:0`, `model:1`, и т.д.
- Сохранена совместимость со старым форматом `model_gemini-2.5-pro` для обратной совместимости
- Список моделей пересоздается в callback в том же порядке, что и при создании клавиатуры

### 3. `app/handlers/callbacks.py` - `register()`

**Было:**
```python
application.add_handler(CallbackQueryHandler(model_button_callback, pattern="^model_"))
```

**Стало:**
```python
application.add_handler(CallbackQueryHandler(model_button_callback, pattern="^model"))
```

**Причина:**
- Паттерн изменен для обработки обоих форматов: `model:` (новый) и `model_` (старый)
- Также обрабатывает `model_none` (разделитель)

## Порядок моделей

Модели добавляются в следующем порядке:
1. Все модели Gemini (`settings.AVAILABLE_MODELS`)
2. Все модели OpenRouter (`settings.OPENROUTER_AVAILABLE_MODELS`)

Индексы:
- 0, 1, 2, ... - модели Gemini
- N, N+1, N+2, ... - модели OpenRouter (где N = количество моделей Gemini)

## Преимущества

1. **Короткий callback_data** - всегда `model:0` вместо `model_cognitivecomputations/dolphin-mistral-24b-venice-edition:free`
2. **Безопасность** - нет риска превысить лимит 64 байта
3. **Обратная совместимость** - старый формат все еще работает
4. **Надежность** - список моделей пересоздается в callback, поэтому порядок всегда правильный

## Примеры

### Новый формат callback_data
- `model:0` - первая модель Gemini
- `model:4` - последняя модель Gemini (если их 5)
- `model:5` - первая модель OpenRouter
- `model:14` - последняя модель OpenRouter (если их 10)

### Старый формат (для совместимости)
- `model_gemini-2.5-pro` - все еще работает
- `model_openai/gpt-4o` - все еще работает (если не превышает 64 байта)

## Тестирование

1. Запустить команду `/model`
2. Проверить, что все кнопки создаются без ошибок
3. Нажать на любую модель (Gemini или OpenRouter)
4. Проверить, что модель корректно выбирается
5. Проверить, что сообщение об успешном выборе модели отображается правильно

