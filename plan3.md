Wait, if `app/utils/messaging.py` modifies the message, what if there's an easier way?
If we define `app/utils/heartbeat.py` with `register_heartbeat` and `stop_heartbeat` functions.

```python
# app/utils/heartbeat.py
import asyncio
from typing import Dict
import logging

_HEARTBEAT_EVENTS: Dict[int, asyncio.Event] = {}

def register_heartbeat(message_id: int, event: asyncio.Event) -> None:
    _HEARTBEAT_EVENTS[message_id] = event

def stop_heartbeat(message_id: int) -> None:
    event = _HEARTBEAT_EVENTS.pop(message_id, None)
    if event and not event.is_set():
        event.set()
```

Then we update `app/handlers/messages.py`:
```python
from app.utils.heartbeat import register_heartbeat, stop_heartbeat
...
        done_event = asyncio.Event()
        register_heartbeat(placeholder_message.message_id, done_event)

        async def _heartbeat() -> None:
             ... # wait logic
             # In finally block or at the end of task wrapper
             # unregister
...
```

And in `app/utils/messaging.py`:
```python
from app.utils.heartbeat import stop_heartbeat

async def send_long_message(...):
    stop_heartbeat(message.message_id)
    ...
```

But what if `send_long_message` is not called?
Errors are handled by `handle_ai_response_error` in `app/handlers/ai_core.py`:
```python
from app.utils.heartbeat import stop_heartbeat

async def handle_ai_response_error(response_text, message, ...):
    stop_heartbeat(message.message_id)
    ...
```
Also in `app/handlers/messages.py` where exceptions are caught:
```python
            except Exception as e:
                stop_heartbeat(placeholder_message.message_id) # done_event is set in finally anyway
                ...
```

Is there anywhere else where the placeholder message is edited?
- `update_stage` in `app/utils/stage_indicators.py`!
If `update_stage` edits the message, it changes the text. Then the heartbeat overwrites it!
Wait, `update_stage` sets something like: "🧠 Модель Gemini думает...". If the heartbeat overwrites this with "⏳ Обрабатываю ваш запрос...", it's not the end of the world, but it erases the stage indicator.
Should `update_stage` stop the heartbeat? If it stops it, the user won't get reassurements if the process gets stuck on the stage for 50s.
Actually, the heartbeat *should* overwrite the stage if it takes too long. That's fine.
BUT what if we make `_WAIT_STAGES` check the current message text? No, it's not cached in telegram bot library easily without a get request.

Let's just stop the heartbeat in:
1. `send_long_message` (app/utils/messaging.py)
2. `send_formatted_message` (app/utils/messaging.py)
3. `edit_formatted_message` (app/utils/messaging.py)
4. `handle_ai_response_error` (app/handlers/ai_core.py)
5. And anywhere `placeholder_message.edit_text` is called for a terminal state.

Wait! A much more robust way is to just wrap `done_event` in the `placeholder_message` object itself, or rather to stop the heartbeat inside `app/utils/messaging.py` when we send the actual message.

Wait, is `app/utils/messaging.py` the only place that sends terminal messages?
Yes, `send_long_message` is exclusively used for sending the AI's response text.

Let's see what happens if I do this.
