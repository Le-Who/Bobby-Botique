1. **Understand the problem**:
   - The user gets a response like "⏳ Обрабатываю ваш запрос..." *after* the model has successfully generated its response.
   - This suggests that a background heartbeat task (which periodically updates the "Thinking..." placeholder message) is overwriting the actual model's response.
   - Specifically, `process_long_request` uses `send_long_message` which might edit `placeholder_message` or reply to it. But meanwhile, the heartbeat task loops through `_WAIT_STAGES` (15s, 30s, 50s). If the model finishes at exactly around 15 seconds, the following race condition occurs:
     - `process_long_request` sends the AI response by editing `placeholder_message`.
     - `done_event.set()` is called immediately *after* `process_long_request` returns.
     - But right before `done_event.set()` is called, the heartbeat `asyncio.wait_for` timeouts (15s), and the heartbeat task proceeds to `await placeholder_message.edit_text(text)`, overwriting the just-sent AI response.

2. **Fix the race condition**:
   - The easiest fix is to pass `done_event` into `process_long_request` or change how the heartbeat behaves. But passing `done_event` all the way down is intrusive.
   - Instead, we can simply set `done_event` *before* the model response is sent, or we can check if `done_event.is_set()` right before `await placeholder_message.edit_text(text)` inside the heartbeat.
   - Wait, `done_event` is set *after* `process_long_request` finishes. If we check `if done_event.is_set():` inside the heartbeat after catching `TimeoutError`, it might still be a race if `process_long_request` is yielding (doing `edit_text`) and the heartbeat wakes up concurrently.
   - The real issue is that `placeholder_message` is being modified by `process_long_request`, and we should not overwrite it if `process_long_request` has already modified it. In `process_long_request` (specifically `_handle_regular_chat`), it edits `placeholder_message` or replies to it.
   - Let's look at `_heartbeat()` again:
     ```python
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=wait_for)
                        return  # Main task finished — stop heartbeat
                    except TimeoutError:
                        pass
                    elapsed = threshold
                    if done_event.is_set():
                        return
                    try:
                        await placeholder_message.edit_text(text)
                    except Exception:
                        pass
     ```
   - Actually, a better and safer fix is to pass the `done_event` or a callable to cancel the heartbeat from inside `process_long_request`? No, the best fix is to stop the heartbeat *before* we start editing the message. But `process_long_request` might take a long time even before it starts sending the response.
   - Wait! The AI response takes 15 seconds to generate (`_get_ai_response_with_routing` takes time). When it finishes, it calls `send_long_message`. `send_long_message` takes some time to edit the message.
   - Let's check `_heartbeat()` in `app/handlers/messages.py`.
   - If we add `if done_event.is_set(): return` inside the `_heartbeat()` right before `edit_text()`, it helps.
   - But if we look at the logs provided by the user:
     `2026-02-28T21:56:46.734840484Z stdout F [02/28/26 21:56:46] INFO Completed task processing for user 6913772015 messages.py:700`
     `2026-02-28T21:56:45.928448479Z stdout F 21:56:45 INFO api_logger log_gemini_response:179`
     `2026-02-28T21:56:31.463904292Z stdout F 21:56:31 INFO api_logger log_gemini_request:130`
     The request started at 21:56:31. The response completed at 21:56:45. So the Gemini response took exactly ~14 seconds. Then sending the message took until 21:56:46.
     The heartbeat threshold is 15 seconds.
     So at exactly 15 seconds (21:56:46), the heartbeat task wakes up and edits the message! At this exact moment, `process_long_request` finishes, and `done_event.set()` is called.

   - To solve this definitively, the heartbeat task should stop the moment the AI finishes generating and starts sending the reply. Or, simply check `done_event.is_set()` before calling `edit_text()`. Even better, we can define a global `placeholder_message.heartbeat_event = done_event` or just let `send_long_message` set something? We can't monkeypatch `Message` easily.

   - Actually, let's look at `_heartbeat`:
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
                     return
                 try:
                     await placeholder_message.edit_text(text)
                 except Exception:
                     pass
     ```
     This check `if done_event.is_set(): return` is good, but if `process_long_request` is currently yielding inside `send_long_message` (e.g. `await message.edit_text()`), then `done_event` is NOT yet set! `done_event.set()` is called *after* `process_long_request` finishes.

     So the race is:
     1. `_heartbeat` wakes up at T=15.
     2. `send_long_message` is inside `process_long_request`, yielding on `await message.edit_text()`.
     3. Both try to edit the same message concurrently.
     4. `send_long_message` succeeds.
     5. `_heartbeat` succeeds.
     6. `process_long_request` returns.
     7. `done_event.set()` is called.

     To fix this, we need to cancel the heartbeat task *before* `process_long_request` starts sending the response, OR we can pass a cancellation callback to `process_long_request`, OR we can set `done_event` inside `process_long_request` right before calling `send_long_message`.
     However, `process_long_request` is a thin facade. It calls `_handle_regular_chat`.
     In `app/handlers/ai_chat.py`, it does:
     ```python
     response_text, new_token_count = await _get_ai_response_with_routing(...)
     # ...
     await send_long_message(placeholder_message, response_text, ...)
     ```
     It would be very easy to stop the heartbeat right before `send_long_message`. We can do this by keeping track of the `done_event` globally or per-message, but passing it around is a pain.

     Let's use a simpler approach. If we just cancel the heartbeat task right after the AI model responds, but *before* we send the message. Since we don't want to change the signature of `process_long_request`, what if we store the heartbeat cancellation in `context.user_data["heartbeat_cancel"]`?

     In `app/handlers/messages.py`:
     ```python
     context.user_data["heartbeat_done"] = done_event
     ```
     And in `app/handlers/ai_chat.py`:
     ```python
     if context and hasattr(context, "user_data") and "heartbeat_done" in context.user_data:
         context.user_data["heartbeat_done"].set()
     ```
     Wait, `_handle_regular_chat` doesn't receive `context`. It receives `placeholder_message, user_id, user_message, chat_state`.
     It doesn't receive `context`.

   - Let's trace `process_long_request` -> `_handle_regular_chat`.
     ```python
     async def process_long_request(
         placeholder_message: Message, update: Update, context: ContextTypes.DEFAULT_TYPE
     ) -> None:
     # ...
         if text.startswith("??"):
             await _handle_research_agent(...)
         elif text.startswith("?"):
             await _handle_qna_search(...)
         elif chat_state.search_enabled:
             await _handle_research_agent(...)
         else:
             await _handle_regular_chat(
                 placeholder_message, update.effective_user.id, text, chat_state
             )
     ```
     None of these receive `context` (except they could, but currently they don't).

   - Another idea: what if `_heartbeat` checks if the text of the `placeholder_message` has been changed by something else? We cannot easily fetch the message text synchronously without an API call.

   - Wait, `send_long_message` in `app/utils/messaging.py` does:
     ```python
     await current_message.edit_text(...)
     ```
     What if we add a global registry `HEARTBEATS = {}` (mapping `message.message_id` -> `asyncio.Event`) in `app.utils.heartbeat`?
     In `app/handlers/messages.py`:
     ```python
     from app.utils.heartbeat import register_heartbeat, stop_heartbeat
     register_heartbeat(placeholder_message.message_id, done_event)
     ```
     And then in `app/utils/messaging.py` inside `send_long_message`, before editing the message:
     ```python
     from app.utils.heartbeat import stop_heartbeat
     stop_heartbeat(message.message_id)
     ```
     This perfectly decouples it! And it handles *any* long message sent that replaces a placeholder!

Let's verify this registry approach.
