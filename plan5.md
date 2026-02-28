Let's see the plan:
1. Create `app/utils/heartbeat.py` with `register_heartbeat` and `stop_heartbeat` functions.
2. In `app/handlers/messages.py`:
   - Import `register_heartbeat`, `stop_heartbeat`.
   - After `done_event = asyncio.Event()`, add `register_heartbeat(placeholder_message.message_id, done_event)`.
   - Update `_heartbeat()` to add `if done_event.is_set(): return` right after `elapsed = threshold`.
   - Add `stop_heartbeat(placeholder_message.message_id)` in the `except` blocks of `task_wrapper` and `except ImportError`.
   - Make sure `finally:` stops heartbeat.
3. In `app/utils/messaging.py`:
   - Add `from app.utils.heartbeat import stop_heartbeat`
   - In `send_long_message`, add `stop_heartbeat(message.message_id)` at the very beginning.
   - In `send_formatted_message`, add `stop_heartbeat(message.message_id)` at the very beginning.
   - In `edit_formatted_message`, add `stop_heartbeat(message.message_id)` at the very beginning.
4. In `app/handlers/ai_core.py`:
   - Add `from app.utils.heartbeat import stop_heartbeat`
   - In `handle_ai_response_error`, add `stop_heartbeat(placeholder_message.message_id)` at the very beginning.
5. In `app/handlers/agent.py`:
   - Add `from app.utils.heartbeat import stop_heartbeat`
   - In `process_long_request`, right before `await placeholder_message.edit_text("Обнаружен сложный запрос ...", ...)` add `stop_heartbeat(placeholder_message.message_id)`.
   - In `process_long_request`, in the `except Exception as e:` block, right before `await placeholder_message.edit_text(friendly, ...)` add `stop_heartbeat(placeholder_message.message_id)`.

This fully resolves the race condition across all scenarios.

Let's do `request_plan_review`.
