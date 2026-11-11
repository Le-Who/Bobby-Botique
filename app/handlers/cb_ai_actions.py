"""
Callback handlers — heavy AI actions.

Handles complex_search, fallback, and retry_last callbacks.
These are semaphore-guarded and run in background tasks.
"""

__all__ = [
    "cancel_long_wait_callback",
    "complex_search_callback",
    "continue_stream_callback",
    "fallback_callback",
    "retry_last_callback",
    "tts_reply_callback",
]

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

from app import state
from app.handlers import agent
from app.handlers.callbacks import (
    _BUSY_TOAST,
    _HEAVY_CALLBACK_SEMAPHORE,
    _background_tasks,
)
from app.i18n import t
from app.repos.chats import get_user_chat
from app.request_context import ensure_request_id as set_request_id
from app.request_context import set_user_context
from app.runtime_settings.additional_prompts import render_additional_prompt
from app.utils.background_tasks import try_submit_task
from app.utils.metrics_middleware import MetricsMiddleware

_CODE_BLOCK_STRIP_RE = re.compile(r"```.*?```", flags=re.DOTALL)
_STALE_CONTINUATION_TOAST = "Этот ответ уже изменён или устарел. Продолжите актуальный ответ."


def _message_ref(message, chat_id=None) -> tuple[int, int] | None:
    message_id = getattr(message, "message_id", None)
    chat_id = getattr(getattr(message, "chat", None), "id", chat_id)
    return (message_id, chat_id) if isinstance(message_id, int) and isinstance(chat_id, int) else None


def _callback_source(query, context):
    original = getattr(query.message, "reply_to_message", None)
    if original is None:
        from app.handlers.retry_requests import message_request

        response_ref = _message_ref(query.message)
        if response_ref is None or state.get_last_bot_message(query.from_user.id) != response_ref:
            return None
        original = (getattr(context, "user_data", None) or {}).get("original_message")
        source = message_request(query.from_user.id)
        if source is not None and (
            source.response_ref != response_ref or source.source_ref != _message_ref(original, response_ref[1])
        ):
            return None
    if getattr(getattr(original, "from_user", None), "id", None) != query.from_user.id:
        return None
    return original


def _continuation_request(query):
    from app.handlers.retry_requests import StaleRetryRequest, current_request, message_request

    user_id = query.from_user.id
    clicked = _message_ref(query.message)
    last = state.get_last_bot_message(user_id)
    source = message_request(user_id)
    request = current_request(user_id)
    if last is None and source is None and request is None:
        return None  # Replies created before ownership metadata was recorded.
    if clicked is None or request is None or request.response_ref != clicked:
        raise StaleRetryRequest("Continuation no longer owns this response")
    if request.source_ref is not None:
        if source is None or source.source_ref != request.source_ref:
            raise StaleRetryRequest("Continuation source changed")
        # The delivery receipt owns split/replacement actions even when the
        # router still remembers this source's original placeholder.
        return request
    if (last is not None and last != clicked) or (source is not None and source.response_ref != clicked):
        raise StaleRetryRequest("Continuation response ownership is uncertain")
    original = _message_ref(getattr(query.message, "reply_to_message", None), clicked[1])
    if source is not None and original is not None and source.source_ref != original:
        raise StaleRetryRequest("Continuation source changed")
    return request


class _CallbackAdmission:
    def __init__(self, user_id: int, lock: asyncio.Lock):
        self.user_id = user_id
        self.lock = lock
        owner = asyncio.current_task()
        assert owner is not None
        self.owner: asyncio.Task = owner
        self.released = False
        state.get_user_state(user_id).is_processing = True
        state.register_active_task(user_id, self.owner)

    def bind_response(self, message) -> None:
        from app.handlers.retry_requests import message_request

        ref = _message_ref(message)
        if ref is not None:
            state.set_last_bot_message(self.user_id, *ref)
            source = message_request(self.user_id)
            if source is not None and source.owner is self.owner:
                source.response_ref = ref

    def transfer(self, task: asyncio.Task, message) -> None:
        from app.handlers.retry_requests import transfer_message_request

        transfer_message_request(self.user_id, self.owner, task)
        self.owner = task
        state.register_active_task(self.user_id, task)
        self.bind_response(message)
        task.add_done_callback(self.release)

    def release(self, owner: asyncio.Task | None) -> None:
        if self.released or self.owner is not owner:
            return
        self.released = True
        if state.get_active_task(self.user_id) is owner:
            state.clear_active_task(self.user_id)
            state.get_user_state(self.user_id).is_processing = False
        self.lock.release()


async def _admit_callback(
    query,
    original=None,
    *,
    validate: Callable[[], Awaitable[bool]] | None = None,
    source_ref: tuple[int, int] | None = None,
) -> _CallbackAdmission | None:
    from app.handlers.retry_requests import message_request, remember_message_request

    user_id = query.from_user.id
    user_lock = state.get_user_lock(user_id)
    user_state = state.get_user_state(user_id)
    owner = state.get_active_task(user_id)
    interaction = user_state.interaction
    if (
        user_lock.locked()
        or user_state.is_processing
        or (owner is not None and not owner.done())
        or (interaction is not None and interaction.task is not None)
    ):
        await query.answer(_BUSY_TOAST)
        return None
    # This acquisition cannot block: no await separates inspection and reserve.
    await user_lock.acquire()
    admission = _CallbackAdmission(user_id, user_lock)
    try:
        if validate is not None and not await validate():
            admission.release(asyncio.current_task())
            await query.answer(_STALE_CONTINUATION_TOAST)
            return None
        source_ref = source_ref or _message_ref(original, getattr(getattr(query.message, "chat", None), "id", None))
        if source_ref is not None:
            remember_message_request(user_id, *source_ref, admission.owner)
        else:
            source = message_request(user_id)
            if source is not None and source.response_ref == _message_ref(query.message):
                source.owner = admission.owner
            else:
                remember_message_request(user_id, None, 0, admission.owner)
        admission.bind_response(query.message)
        await query.answer()
    except BaseException:
        admission.release(asyncio.current_task())
        raise
    return admission


async def _schedule_ai_callback(coroutine, placeholder, *, operation: str, nested=None) -> asyncio.Task | None:
    from app.runtime_settings.lifecycle import current_operation_snapshot, runtime_settings_scope

    snapshot = current_operation_snapshot()

    async def request_worker():
        if snapshot is None:
            await coroutine
        else:
            # This is admitted request work; retain its route revision while
            # the shared manager detaches ordinary background-job snapshots.
            async with runtime_settings_scope(snapshot):
                await coroutine

    task = try_submit_task(request_worker(), operation=operation)
    if task is None:
        coroutine.close()
        if nested is not None:
            nested.close()
        await placeholder.edit_text(t("role.server_overloaded"))
        return None
    task.add_done_callback(lambda _task: coroutine.close())
    if nested is not None:
        task.add_done_callback(lambda _task: nested.close())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _strip_tts_code_blocks(text: str) -> str:
    return _CODE_BLOCK_STRIP_RE.sub("", text)


async def complex_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    set_user_context(
        query.from_user.id,
        getattr(query.message.chat, "id", None) if query.message else None,
    )

    action = query.data.split(":")[1]
    placeholder_message = query.message

    if action == "cancel":
        await query.answer()
        await placeholder_message.delete()
        return

    original_message = _callback_source(query, context)

    if not original_message:
        from app.utils.keyboards import error_with_back_keyboard

        await query.answer()
        await placeholder_message.edit_text(
            t("error.original_not_found"),
            reply_markup=error_with_back_keyboard("start_menu", "⬅️ Меню"),
        )
        return

    user_id = query.from_user.id
    admission = await _admit_callback(query, original_message)
    if admission is None:
        return
    try:
        # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
        # 1. Определяем, какую задачу будем запускать.
        task_to_run = None
        if action == "vision_only":
            # 2. СРАЗУ даем обратную связь пользователю.
            await placeholder_message.edit_text(t("processing.describing_image"))
            chat_state = await get_user_chat(user_id)
            task_to_run = agent._handle_photo(placeholder_message, original_message, chat_state)  # type: ignore[arg-type]  # message comes from original update
        elif action == "confirm":
            # У этой функции своя обратная связь ("Аналfromирую..."), поэтому здесь ничего не меняем.
            search_prefix = "??" if (original_message.caption and original_message.caption.startswith("??")) else "?"
            task_to_run = agent._handle_complex_agent_search(placeholder_message, original_message, search_prefix)

        # 3. If задача определена, запускаем ее в фоне под блокировкой.
        if task_to_run:

            async def task_wrapper() -> None:
                try:
                    from app.adapters.concurrency import ultra_heavy_semaphore

                    async with (
                        ultra_heavy_semaphore,
                        MetricsMiddleware("complex_search_callback", user_id=user_id),
                    ):
                        await task_to_run
                except Exception as e:
                    logging.error("complex_search task failed: %s", e, exc_info=True)
                    with contextlib.suppress(Exception):
                        await placeholder_message.edit_text(t("error.generic"))

            task = await _schedule_ai_callback(
                task_wrapper(), placeholder_message, operation="callback.complex_search", nested=task_to_run
            )

            if task is not None:
                admission.transfer(task, placeholder_message)
    finally:
        admission.release(asyncio.current_task())


async def fallback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    set_user_context(
        query.from_user.id,
        getattr(query.message.chat, "id", None) if query.message else None,
    )

    parts = query.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    model_override = parts[2] if len(parts) > 2 else ""
    placeholder_message = query.message

    if action == "cancel":
        await query.answer()
        await placeholder_message.edit_text(t("btn.operation_cancelled"))
        return

    original_message = _callback_source(query, context)

    if not original_message:
        from app.utils.keyboards import error_with_back_keyboard

        await query.answer()
        await placeholder_message.edit_text(
            t("error.original_not_found"),
            reply_markup=error_with_back_keyboard("start_menu", "⬅️ Меню"),
        )
        return

    user_id = query.from_user.id
    admission = await _admit_callback(query, original_message)
    if admission is None:
        return
    try:

        async def task_wrapper() -> None:
            try:
                async with _HEAVY_CALLBACK_SEMAPHORE:
                    if action == "confirm":
                        user_message = original_message.text
                        # ⚡ Bolt Optimization: Fetch chat state and detect TTS intent concurrently
                        from app.voice_intent import detect_tts_intent

                        chat_state, voice_decision = await asyncio.gather(
                            get_user_chat(user_id), detect_tts_intent(user_text=user_message)
                        )

                        async with MetricsMiddleware("fallback_callback", user_id=user_id):
                            await agent._handle_regular_chat(
                                placeholder_message,  # type: ignore[arg-type]  # MaybeInaccessibleMessage
                                user_id,
                                user_message,
                                chat_state,
                                model_override=model_override,
                                reply_with_voice=voice_decision.explicit_tts,
                            )
            except Exception as e:
                logging.error("fallback task failed: %s", e, exc_info=True)
                with contextlib.suppress(Exception):
                    await placeholder_message.edit_text(t("error.generic"))

        task = await _schedule_ai_callback(task_wrapper(), placeholder_message, operation="callback.fallback")

        if task is not None:
            admission.transfer(task, placeholder_message)
    finally:
        admission.release(asyncio.current_task())


async def cancel_long_wait_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel only the matching, still-pending reply before enabling retry."""
    del context
    query = update.callback_query
    if not query or not update.effective_user or not query.message or not query.data:
        return
    user_id = update.effective_user.id
    language_code = getattr(getattr(query, "from_user", None), "language_code", "") or ""
    lang = "en" if language_code.lower().startswith("en") else "ru"
    chat_id = getattr(getattr(query.message, "chat", None), "id", None)
    message_id = getattr(query.message, "message_id", None)
    expected = f"retry_wait:{message_id}"
    from app.utils.heartbeat import is_heartbeat_active

    placeholder_owner = state.get_last_bot_message(user_id)
    if (
        query.data != expected
        or placeholder_owner != (message_id, chat_id)
        or not is_heartbeat_active(message_id, chat_id=chat_id)
    ):
        await query.answer(t("processing.long_wait_finished", lang))
        return
    owner = state.get_active_task(user_id)
    if owner is None or not state.cancel_active_task(user_id):
        await query.answer(t("processing.long_wait_finished", lang))
        return

    try:
        async with asyncio.timeout(5.0):
            # A released lock can precede worker finally/resource callbacks.
            completion: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            owner.add_done_callback(lambda _owner: completion.set_result(None))
            await asyncio.shield(completion)
            async with state.get_user_lock(user_id):
                pass
    except TimeoutError:
        await query.answer(t("processing.long_wait_stopping", lang))
        return

    def superseded() -> bool:
        return (
            state.get_active_task(user_id) not in (None, owner)
            or state.get_last_bot_message(user_id) is not placeholder_owner
        )

    if superseded():
        await query.answer(t("processing.long_wait_finished", lang))
        return

    from app.errors import build_retry_and_roles_keyboard

    await query.answer(t("processing.long_wait_stopped", lang))
    if superseded():
        return
    await query.edit_message_text(
        t("processing.long_wait_retry_prompt", lang),
        reply_markup=build_retry_and_roles_keyboard(include_roles=False),
    )


async def retry_last_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Replay the owned logical request, preserving its original chat/search route."""
    del context
    query = update.callback_query
    if query is None or query.message is None:
        return
    user_id = query.from_user.id
    set_request_id(f"tgcb-{user_id}-{query.id}")
    set_user_context(user_id, getattr(query.message.chat, "id", None))
    from app.handlers.retry_requests import (
        StaleRetryRequest,
        current_request,
        legacy_request,
        message_request,
        retry_history,
        scope_matches,
    )
    from app.state import ensure_state_loaded, get_last_sent_message

    await ensure_state_loaded(user_id)
    identity = (query.data or "").partition(":")[2]
    legacy_text = get_last_sent_message(user_id) if not identity else None
    if not identity and current_request(user_id) is None and not legacy_text:
        from app.utils.keyboards import error_with_back_keyboard

        await query.answer()
        await query.edit_message_text(
            t("error.no_retry_data"), reply_markup=error_with_back_keyboard("start_menu", "⬅️ Меню")
        )
        return

    user_lock = state.get_user_lock(user_id)
    owner = state.get_active_task(user_id)
    interaction = state.get_user_state(user_id).interaction
    if (
        user_lock.locked()
        or (owner is not None and not owner.done())
        or (interaction is not None and interaction.task is not None)
    ):
        await query.answer(_BUSY_TOAST)
        return
    # No intervening await after the check: acquisition and reservation are one
    # event-loop admission; the worker inherits this lock until its finally.
    await user_lock.acquire()
    handed_off = False
    request = None
    try:
        chat_state = await get_user_chat(user_id)
        request = current_request(user_id)
        source_ref = (query.message.message_id, query.message.chat.id)
        if identity:
            if request is None or request.identity != identity or request.response_ref != source_ref:
                raise StaleRetryRequest("Retry no longer owns this response")
        elif request is not None and request.response_ref is not None and request.response_ref != source_ref:
            raise StaleRetryRequest("Legacy retry no longer owns this response")
        if request is None:
            if not legacy_text:
                raise StaleRetryRequest("Retry data expired")
            request = legacy_request(user_id, legacy_text, chat_state)
        if not scope_matches(request, chat_state) or not request.replayable:
            raise StaleRetryRequest("Retry generation or branch changed")
        if request.running:
            await query.answer(_BUSY_TOAST)
            return
        if request.kind == "chat":
            retry_history(request, chat_state.history)
        request.running = True
        await query.answer()
        reply_text = getattr(query.message, "reply_text", None)
        if not callable(reply_text):
            return
        placeholder_message = await reply_text(t("processing.retry"))
        state.set_last_bot_message(user_id, placeholder_message.message_id, placeholder_message.chat.id)
        replay = request
        response_ref_before_attempt = replay.response_ref
        released = False
        task = None

        def release_admission() -> None:
            nonlocal released
            if released:
                return
            released = True
            replay.running = False
            if state.get_active_task(user_id) is task:
                state.clear_active_task(user_id)
            user_lock.release()

        async def _retry_wrapper() -> None:
            try:
                async with _HEAVY_CALLBACK_SEMAPHORE, MetricsMiddleware("retry_last_callback", user_id=user_id):
                    if current_request(user_id) is not replay or not scope_matches(replay, chat_state):
                        raise StaleRetryRequest("Retry was superseded before execution")
                    if replay.kind == "search":
                        await agent._handle_qna_search(
                            placeholder_message,
                            replay.user_text,
                            chat_state,
                            search_query=replay.search_query,
                            user_id=user_id,
                            retry_request=replay,
                        )
                    else:
                        await agent._handle_regular_chat(
                            placeholder_message,
                            user_id,
                            replay.user_text,
                            chat_state,
                            retry_request=replay,
                            capture_text_memory=replay.capture_text_memory,
                            is_forward_batch=replay.is_forward_batch,
                            reply_with_voice=replay.reply_with_voice,
                            user_parts=list(replay.text_parts) if replay.text_parts is not None else None,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.error("retry_last_callback failed (error_type=%s)", type(error).__name__)
                if replay.response_ref != response_ref_before_attempt:
                    # Delivery already owns the final answer/actions. Later
                    # persistence failures must not replace that presentation.
                    return
                with contextlib.suppress(Exception):
                    from app.errors import build_retry_and_roles_keyboard

                    await placeholder_message.edit_text(
                        t("error.retry_failed"),
                        reply_markup=build_retry_and_roles_keyboard(retry_callback=replay.callback_data),
                    )
                    replay.response_ref = (placeholder_message.message_id, placeholder_message.chat.id)
            finally:
                release_admission()

        task = await _schedule_ai_callback(_retry_wrapper(), placeholder_message, operation="callback.retry")
        if task is None:
            return
        source = message_request(user_id)
        if source is not None and source.response_ref == source_ref:
            source.owner = task
            source.response_ref = (placeholder_message.message_id, placeholder_message.chat.id)
        state.register_active_task(user_id, task)
        task.add_done_callback(lambda _: release_admission())
        handed_off = True
    except StaleRetryRequest:
        await query.answer("Этот запрос уже изменён или устарел. Отправьте его снова.", show_alert=True)
    finally:
        if not handed_off:
            if request is not None:
                request.running = False
            user_lock.release()


async def tts_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate voice reply for the current AI response (🔊 Озвучить button)."""
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    set_user_context(
        query.from_user.id,
        getattr(query.message.chat, "id", None) if query.message else None,
    )

    # Extract text from the message that has the button
    response_text = query.message.text if query.message else None

    # Check if there's a WebApp url with a UID (Long-read)
    if query.message and query.message.reply_markup:
        for row in query.message.reply_markup.inline_keyboard:
            for btn in row:
                if btn.web_app and "/reader?id=" in btn.web_app.url:
                    uid = btn.web_app.url.split("/reader?id=")[1].split("&")[0]
                    try:
                        from app.cache import get_long_message

                        full_text = await get_long_message(uid)
                        if full_text:
                            response_text = full_text
                            logging.info(
                                "tts_reply_callback: Fetched full text (%d chars) for uid=%s", len(full_text), uid
                            )
                    except Exception as e:
                        logging.warning("tts_reply_callback: Failed to load long message from cache: %s", e)
                    break

    if not response_text or len(response_text.strip()) < 5:
        return

    # Strip code blocks so TTS does not synthesize visually represented source code.
    response_text = _strip_tts_code_blocks(response_text)

    chat_id = query.message.chat_id
    message_id = query.message.message_id

    try:
        from app.repos.chats import get_user_chat

        chat_state = await get_user_chat(query.from_user.id)
        from app.voice_engine import fire_voice_reply
        from app.voice_intent import build_voice_source_key

        enqueue_result = await fire_voice_reply(
            bot=query.get_bot(),
            user_id=query.from_user.id,
            chat_id=chat_id,
            reply_to_message_id=message_id,
            response_text=response_text,
            voice=chat_state.voice_id or "",
            tts_temperature=chat_state.tts_temperature,
            source_key=build_voice_source_key("tts_button", chat_id, message_id),
        )
        if enqueue_result.deduped:
            feedback = "🔊 Озвучка уже в очереди"
        elif enqueue_result.queued:
            feedback = "🔊 Генерирую голос..."
        else:
            feedback = {
                "capacity": "⏳ Очередь озвучки заполнена. Попробуйте позже.",
                "user_capacity": "⏳ Дождитесь завершения вашей очереди озвучки.",
                "text_too_large": "❌ Текст слишком длинный для озвучки.",
                "closed": "⏳ Озвучка временно недоступна. Попробуйте позже.",
            }.get(enqueue_result.reason_code, "❌ Озвучка недоступна. Обновите ответ и попробуйте снова.")
        await query.answer(feedback)
    except Exception as e:
        logging.error("TTS reply callback failed: %s", e)
        await query.answer("❌ Ошибка озвучки")


# ── Interruption error footers to strip from partial text ──────────────────
_INTERRUPTION_FOOTERS = (
    "\n\n⏰ _(ответ был прерван по таймауту)_",
    "\n\n⚠️ _(ответ был прерван из-за ошибки сервера)_",
    "\n\n⚠️ _(ответ был прерван из-за непредвиденной ошибки)_",
    # Legacy footers (backward compat)
    "\n\n⚠️ _(ответ был прерван из-за ошибки API)_",
)


def _strip_interruption_footer(text: str) -> str:
    """Remove the machine-appended interruption footer from partial text."""
    for footer in _INTERRUPTION_FOOTERS:
        if text.endswith(footer):
            return text[: -len(footer)]
    return text


async def continue_stream_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Continue generating a response from where it was interrupted (▶️ Продолжить button)."""
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    set_user_context(
        query.from_user.id,
        getattr(query.message.chat, "id", None) if query.message else None,
    )
    user_id = query.from_user.id

    # Extract partial text from the interrupted message
    partial_text = query.message.text if query.message else None
    if not partial_text or len(partial_text.strip()) < 10:
        await query.answer("❌")
        return

    # Strip the interruption footer to get clean partial text
    clean_partial = _strip_interruption_footer(partial_text)

    from app.handlers.retry_requests import StaleRetryRequest, current_request, message_request, scope_matches

    try:
        request = _continuation_request(query)
    except StaleRetryRequest:
        await query.answer(_STALE_CONTINUATION_TOAST)
        return
    chat_state = None
    legacy_scope: tuple[int, object | None] | None = None

    async def validate_continuation() -> bool:
        nonlocal chat_state, legacy_scope
        current = await get_user_chat(user_id)
        if current is None or current_request(user_id) is not request:
            return False
        if request is not None:
            if request.response_ref != _message_ref(query.message) or not scope_matches(request, current):
                return False
            if request.source_ref is not None:
                source = message_request(user_id)
                if source is None or source.source_ref != request.source_ref:
                    return False
        else:
            if getattr(current, "private_data_blocked", False):
                return False
            scope = (int(getattr(current, "memory_epoch", 0) or 0), getattr(current, "branch_id", None))
            if legacy_scope is not None and legacy_scope != scope:
                return False
            legacy_scope = scope
        chat_state = current
        return True

    admission = await _admit_callback(
        query,
        getattr(query.message, "reply_to_message", None),
        validate=validate_continuation,
        source_ref=request.source_ref if request is not None else None,
    )
    if admission is None:
        return
    try:
        # Create placeholder for the continuation response
        placeholder_message = await query.message.reply_text(t("processing.continuing"))

        from app.handlers.agent import _handle_regular_chat

        # Captured here so the inner closure can reference them without closure mutation risk
        _clean_partial = clean_partial
        _continuation_prompt = render_additional_prompt("chat.continue")

        async def _continue_wrapper() -> None:
            try:
                async with _HEAVY_CALLBACK_SEMAPHORE:
                    # Fetch and mutate state under lock to prevent history data race
                    from app.metrics import role_conv_metrics
                    from app.utils.background_tasks import submit_task

                    if not await validate_continuation():
                        await placeholder_message.edit_text(_STALE_CONTINUATION_TOAST)
                        return
                    _chat_state = chat_state
                    assert _chat_state is not None
                    last_turn = _chat_state.history[-1] if _chat_state.history else None
                    if not last_turn or last_turn.get("role") != "model":
                        # Compatibility for interrupted replies created before
                        # partial deliveries were persisted. Current deliveries
                        # already end in a model turn. Comparing rendered Telegram
                        # text is unsafe because formatting entities are normalized.
                        _chat_state.history.append({"role": "model", "parts": [_clean_partial]})
                    submit_task(role_conv_metrics.record_stream_recovery())

                    async with MetricsMiddleware("continue_stream_callback", user_id=user_id):
                        await _handle_regular_chat(
                            placeholder_message,
                            user_id,
                            _continuation_prompt,
                            _chat_state,
                        )
            except Exception as e:
                logging.error("continue_stream_callback failed: %s", e, exc_info=True)
                try:
                    from app.utils.keyboards import error_with_back_keyboard

                    await placeholder_message.edit_text(
                        t("error.retry_failed"),
                        reply_markup=error_with_back_keyboard("start_menu", "⬅️ Меню"),
                    )
                except Exception:
                    pass

        task = await _schedule_ai_callback(_continue_wrapper(), placeholder_message, operation="callback.continue")

        if task is not None:
            admission.transfer(task, placeholder_message)
    finally:
        admission.release(asyncio.current_task())
