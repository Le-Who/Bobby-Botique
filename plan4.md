Let's see:
```python
# app/utils/heartbeat.py
import asyncio
from typing import Dict
import logging

_HEARTBEAT_EVENTS: Dict[int, asyncio.Event] = {}

def register_heartbeat(message_id: int, event: asyncio.Event) -> None:
    _HEARTBEAT_EVENTS[message_id] = event

def unregister_heartbeat(message_id: int) -> None:
    _HEARTBEAT_EVENTS.pop(message_id, None)

def stop_heartbeat(message_id: int) -> None:
    event = _HEARTBEAT_EVENTS.pop(message_id, None)
    if event and not event.is_set():
        event.set()
        logging.info(f"Heartbeat manually stopped for message {message_id}")
```
In `app/handlers/messages.py`:
```python
from app.utils.heartbeat import register_heartbeat, unregister_heartbeat
...
        done_event = asyncio.Event()
        register_heartbeat(placeholder_message.message_id, done_event)

        async def _heartbeat() -> None:
...
            finally:
                # Ensure heartbeat is stopped even on exception paths
                unregister_heartbeat(placeholder_message.message_id)
                if not done_event.is_set():
...
```
In `app/utils/messaging.py`:
```python
from app.utils.heartbeat import stop_heartbeat

async def send_long_message(message: Message, ...):
    stop_heartbeat(message.message_id)
    ...

async def send_formatted_message(message: Message, ...):
    stop_heartbeat(message.message_id)
    ...

async def edit_formatted_message(message: Message, ...):
    stop_heartbeat(message.message_id)
    ...
```

In `app/handlers/ai_core.py`:
```python
from app.utils.heartbeat import stop_heartbeat

async def handle_ai_response_error(
    response_text: str,
    placeholder_message: Message,
...
):
    stop_heartbeat(placeholder_message.message_id)
...
```

Wait, `app/handlers/messages.py` lines 685-694:
```python
                        # Восстанавливаем обработку via agent
                        try:
                            from app.handlers.agent import process_long_request

                            await process_long_request(placeholder_message, update, context)
                        except ImportError:
                            # Fallback if agent недоступен
                            from app.utils.heartbeat import stop_heartbeat
                            stop_heartbeat(placeholder_message.message_id)
                            await placeholder_message.edit_text(
                                "🤔 Обрабатываю ваш запрос... (упрощенный режим)"
                            )

                        # Stop heartbeat IMMEDIATELY after response is sent
                        # to prevent it from overwriting the AI answer.
                        done_event.set()
                        heartbeat_task.cancel()
```
Also `app/handlers/messages.py` line 716 catch:
```python
            except Exception as e:
                logging.error(...)
                try:
                    from app.utils.heartbeat import stop_heartbeat
                    stop_heartbeat(placeholder_message.message_id)
                    from app.errors import build_retry_and_roles_keyboard
                    await placeholder_message.edit_text(...)
```

This ensures we NEVER overwrite a terminal response!

Let's double check if `process_long_request` handles complex requests and shows an inline keyboard:
`app/handlers/agent.py`:
```python
from app.utils.heartbeat import stop_heartbeat

...
            # Не удаляем placeholder message, а редактируем его
            try:
                stop_heartbeat(placeholder_message.message_id)
                await placeholder_message.edit_text(
                    "Обнаружен сложный запрос ...",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
```
And also background task dispatcher exception catch:
```python
    except Exception as e:
        ...
            stop_heartbeat(placeholder_message.message_id)
            await placeholder_message.edit_text(
                friendly, reply_markup=build_retry_and_roles_keyboard()
            )
```
Wait, if `app/handlers/agent.py` also calls `stop_heartbeat`, we are fully covered.

Is this safe? Yes.
Does this solve the problem? Yes, the race condition happens exactly because the terminal response doesn't stop the heartbeat before it yields for `edit_text` or `reply_text`. This `stop_heartbeat` will set the `done_event`, effectively aborting any `wait_for` inside the heartbeat BEFORE the network call for `edit_text` starts in `send_long_message`.

Let's review the code in `_heartbeat`:
```python
        async def _heartbeat() -> None:
            try:
                elapsed = 0
                for threshold, text in _WAIT_STAGES:
                    wait_for = threshold - elapsed
                    if wait_for <= 0:
                        continue
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=wait_for)
                        return  # Main task finished — stop heartbeat
                    except TimeoutError:
                        pass
                    elapsed = threshold

                    if done_event.is_set():
                        return # <--- Need to add this to be safe!

                    try:
                        await placeholder_message.edit_text(text)
                    except Exception:
                        pass  # Message already edited by main task or deleted
```
If we add `if done_event.is_set(): return` before `edit_text`, we prevent the heartbeat from editing the message right after the timeout, if the done_event was set simultaneously! Wait, if `stop_heartbeat` sets `done_event` right before `send_long_message` calls `edit_text`, then `_heartbeat` which was suspended on `wait_for` might have JUST timed out, and is currently scheduled to run. So `done_event.is_set()` check inside `_heartbeat` is CRITICAL.

Actually, what if `send_long_message` calls `stop_heartbeat()`, which calls `done_event.set()`.
If `_heartbeat` caught the `TimeoutError`, it is executing `elapsed = threshold` and then `await placeholder_message.edit_text(text)`.
If we check `if done_event.is_set(): return`, it will definitely be safe.

Let's write a python test to verify this race condition fix.
