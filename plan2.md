Let's see where else `placeholder_message` is modified after it is created.
In `app/handlers/ai_chat.py`:
```python
    try:
        await update_stage(placeholder_message, STAGES_CHAT, 0)
    except Exception as edit_error:
...
    # ... Wait for Gemini response
    response_text, new_token_count = await _get_ai_response_with_routing(...)

    if response_text:
        # Check if error ...
        if await handle_ai_response_error(
            response_text, placeholder_message, on_error_callback=cleanup_on_error
        ):
            return  # Error processed
        else:
            # SUCCEEDED -> sending response
            await send_long_message(
                placeholder_message, response_text, reply_markup=reply_markup
            )
```
Wait, `update_stage` is called BEFORE the AI response. `update_stage` in `app/utils/stage_indicators.py` also edits the message (adds "🧠 Модель {model_used} думает...").
Wait, if `update_stage` is called, it *changes* the text. The heartbeat will then overwrite it! This is already a bug: the heartbeat will overwrite the stage indicator text!

Also, `handle_ai_response_error` edits the placeholder message. So we should probably stop the heartbeat right before *any* of these things happen.

Actually, when is the heartbeat supposed to run?
If we stop the heartbeat inside `_get_ai_response_with_routing`? No, the heartbeat is *meant* to run while waiting for the AI response.
Wait, `update_stage` is currently modifying the message:
`await update_stage(placeholder_message, STAGES_CHAT, 0)` which is just a static string, probably.
If the heartbeat overwrites the stage indicator, that's not terrible while waiting, but it's bad when `send_long_message` completes and then the heartbeat overwrites the *final answer*.

To ensure the heartbeat NEVER overwrites the final answer (or any error message), we can stop it inside `send_long_message`, `send_formatted_message`, `edit_formatted_message`, and `handle_ai_response_error`.
But why not just stop it in `_handle_regular_chat` right after `_get_ai_response_with_routing` returns? Because `_handle_regular_chat` doesn't have access to the `done_event`.

Wait, we can create an `unregister_heartbeat` which also stops the heartbeat.
```python
# app/utils/heartbeat.py
import asyncio
from typing import Dict

_HEARTBEAT_EVENTS: Dict[int, asyncio.Event] = {}

def register_heartbeat(message_id: int, event: asyncio.Event) -> None:
    _HEARTBEAT_EVENTS[message_id] = event

def unregister_heartbeat(message_id: int) -> None:
    _HEARTBEAT_EVENTS.pop(message_id, None)

def stop_heartbeat(message_id: int) -> None:
    event = _HEARTBEAT_EVENTS.pop(message_id, None)
    if event:
        event.set()
```

Where to call `stop_heartbeat`?
In `app/utils/messaging.py`:
```python
from app.utils.heartbeat import stop_heartbeat

async def send_long_message(...):
    stop_heartbeat(message.message_id)
    ...

async def send_formatted_message(message: Message, ...):
    stop_heartbeat(message.message_id)
    ...

async def edit_formatted_message(message: Message, ...):
    stop_heartbeat(message.message_id)
    ...
```

Wait, `app/handlers/agent.py` also modifies `placeholder_message`:
```python
            await placeholder_message.edit_text(
                "Обнаружен сложный запрос ...",
            )
```
If we call `stop_heartbeat(placeholder_message.message_id)` there, it will stop it. But instead of adding `stop_heartbeat` to every place that calls `edit_text()`, what if we patch or wrap `edit_text`? No, that's dangerous.
What if we pass `done_event` in `context.user_data`? We *can* inject it!
In `app/handlers/messages.py`:
```python
context.user_data["current_heartbeat_event"] = done_event
```
But `app/handlers/ai_chat.py` does not receive `context`.
`app/handlers/ai_search.py` does not receive `context`.

Let's look at `app/utils/messaging.py`. It is a central place where messages are sent.
If we put `stop_heartbeat(message.message_id)` inside `send_long_message`, that will prevent the most critical bug: overwriting the final AI answer.

Are there other places that edit the placeholder with the final answer?
- `_handle_regular_chat`: `send_long_message(placeholder_message, response_text, ...)`
- `_handle_document_question`: `send_long_message(placeholder_message, response_text, ...)`
- `_handle_photo`: `send_long_message(placeholder_message, response_text, ...)`
- `_handle_qna_search`: `send_long_message(placeholder_message, response_text, ...)`
- `_handle_research_agent`: `send_long_message(placeholder_message, final_response, ...)`

So EVERY successful flow calls `send_long_message`.
What about errors?
- `handle_ai_response_error` inside `app/handlers/ai_core.py` calls `placeholder_message.edit_text(...)` or `reply_text(...)`.
- The `try/except` inside `app/handlers/messages.py` catches global errors and edits the message to "❌ Произошла ошибка..."

If we add `stop_heartbeat(message.message_id)` to `send_long_message` AND `handle_ai_response_error`, we cover all final states.

Wait, why doesn't `_heartbeat` just check the current text of the message? It can't, `edit_text` doesn't fetch the current text. It just overwrites it.

What if we store the `message.message_id` as the key in `HEARTBEATS`? Yes.
```python
# app/utils/heartbeat.py
import asyncio
from typing import Dict
import logging

_HEARTBEAT_EVENTS: Dict[int, asyncio.Event] = {}

def register_heartbeat(message_id: int, event: asyncio.Event) -> None:
    _HEARTBEAT_EVENTS[message_id] = event
    logging.debug(f"Registered heartbeat for message {message_id}")

def stop_heartbeat(message_id: int) -> None:
    event = _HEARTBEAT_EVENTS.pop(message_id, None)
    if event and not event.is_set():
        event.set()
        logging.debug(f"Stopped heartbeat for message {message_id}")
```
