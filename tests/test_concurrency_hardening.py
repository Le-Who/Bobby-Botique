import asyncio
import importlib
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


def test_role_custom_retry_registered_once():
    text = Path("app/handlers/callbacks.py").read_text(encoding="utf-8")
    assert text.count('pattern="^role_custom_retry$"') == 1


def test_heavy_callback_semaphore_present_and_used():
    # Semaphore is defined in callbacks.py (shared helpers hub)
    hub_source = Path("app/handlers/callbacks.py").read_text(encoding="utf-8")
    assert "_HEAVY_CALLBACK_SEMAPHORE = asyncio.Semaphore" in hub_source

    # Semaphore is used in cb_ai_actions.py (heavy callbacks)
    actions_source = Path("app/handlers/cb_ai_actions.py").read_text(encoding="utf-8")
    async_with_count = actions_source.count("async with _HEAVY_CALLBACK_SEMAPHORE")
    assert async_with_count >= 2


def test_heavy_message_semaphore_present_and_used():
    # Multi-tier semaphore: both tiers exported from concurrency.py
    concurrency_source = Path("app/adapters/concurrency.py").read_text(encoding="utf-8")
    assert "heavy_request_semaphore = _LazyGlobalLLMSemaphore" in concurrency_source
    assert "ultra_heavy_semaphore = _LazyGlobalLLMSemaphore" in concurrency_source

    # Semaphores are acquired inside agent.py's process_long_request
    agent_source = Path("app/handlers/agent.py").read_text(encoding="utf-8")
    assert "ultra_heavy_semaphore" in agent_source
    assert "heavy_request_semaphore" in agent_source

    # media-group heavy path still has its own semaphore
    media_source = Path("app/handlers/msg_media.py").read_text(encoding="utf-8")
    assert "async with _HEAVY_REQUEST_SEMAPHORE" in media_source


# ── Streaming lock guard tests ───────────────────────────────────────────────


def test_is_user_busy_helper_exists():
    """Verify the _is_user_busy helper is defined in callbacks.py."""
    source = Path("app/handlers/callbacks.py").read_text(encoding="utf-8")
    assert "def _is_user_busy(user_id" in source
    assert "_BUSY_TOAST" in source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name,handler_name,data,initial_answer",
    [
        ("cb_models", "model_button_callback", "model:0", call()),
        ("cb_models", "switch_model_callback", "switch_model:gemini-3.1-flash-lite", call()),
        ("cb_navigation", "new_topic_callback", "new_topic", call("...")),
        ("cb_navigation", "new_chat_callback", "new_chat", None),
        ("cb_navigation", "deep_dive_callback", "deep_dive:new_topic", call()),
        ("cb_navigation", "toggle_search_callback", "toggle_search", None),
    ],
    ids=["model", "switch_model", "new_topic", "new_chat", "deep_dive", "toggle_search"],
)
async def test_busy_state_mutating_callbacks_have_no_effects(module_name, handler_name, data, initial_answer):
    from app.handlers.callbacks import _BUSY_TOAST

    module = importlib.import_module(f"app.handlers.{module_name}")
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.from_user.id = 73421
    update.effective_user.language_code = "ru"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {"model_list": ["gemini-3.1-flash-lite"], "sentinel": "keep"}
    before = deepcopy(context.user_data)
    lock = asyncio.Lock()
    await lock.acquire()
    with ExitStack() as stack:
        lock_reader = stack.enter_context(patch("app.handlers.callbacks.state.get_user_lock", return_value=lock))
        get_chat = stack.enter_context(patch.object(module, "get_user_chat", new_callable=AsyncMock))
        update_chat = stack.enter_context(patch.object(module, "update_user_chat", new_callable=AsyncMock))
        menus = stack.enter_context(patch.object(module, "menus"))
        submit = stack.enter_context(patch("app.utils.background_tasks.submit_task"))
        try:
            await getattr(module, handler_name)(update, context)
        finally:
            lock.release()

    lock_reader.assert_called_once_with(73421)
    expected_answers = [] if initial_answer is None else [initial_answer]
    assert query.answer.await_args_list == expected_answers + [call(_BUSY_TOAST, show_alert=True)]
    get_chat.assert_not_called()
    update_chat.assert_not_called()
    assert menus.mock_calls == []
    submit.assert_not_called()
    query.edit_message_text.assert_not_called()
    query.edit_message_reply_markup.assert_not_called()
    query.message.reply_text.assert_not_called()
    assert context.user_data == before
