"""Tests for app.handlers.ai_chat — regular conversational AI chat."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.errors import ErrorCode
from app.handlers.ai_chat import _build_chat_response_markup, _handle_regular_chat
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GroundingReport,
    KeyDisposition,
    ProviderKind,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamFailed,
    TokenUsage,
)
from app.response_delivery.outcomes import (
    CompleteDelivery,
    FailedDelivery,
    PartialDelivery,
)
from app.response_delivery.presentation import PresentationFacts
from app.response_delivery.renderer import (
    DeliveryKind,
    DeliveryReceipt,
    TelegramMessageRef,
)
from tests.factories import make_chat_state, make_telegram_message


def _receipt(message_id: int = 55) -> DeliveryReceipt:
    return DeliveryReceipt(
        kind=DeliveryKind.MESSAGE,
        message_ids=(message_id,),
        final_message=TelegramMessageRef(chat_id=456, message_id=message_id),
    )


def _completion(total_tokens: int = 42) -> StreamCompleted:
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=total_tokens),
        grounding=GroundingReport(),
        route=RouteUsed(
            provider=ProviderKind.GEMINI,
            requested_model="gemini-3.1-flash-lite",
            actual_model="gemini-3.1-flash-lite",
        ),
    )


@pytest.fixture
def mock_boundaries():
    """Setup external boundaries for AI Chat handler with strict AAA isolation.

    We ONLY mock the actual external dependencies:
    1. The DB persistence layer (update_user_chat)
    2. The typed Telegram response-delivery boundary
    3. The model resolution boundary (_resolve_ai_request) to simulate specific provider states
       without doing real DB lookups.
    4. Minor cosmetic indicators (update_stage, search_memories, metrics)
    """

    with (
        patch("app.handlers.ai_chat.update_user_chat", new_callable=AsyncMock) as m_update_chat,
        patch("app.handlers.ai_chat._resolve_ai_request", new_callable=AsyncMock) as m_resolve,
        patch("app.response_delivery.delivery.get_telegram_response_delivery") as get_delivery,
        # Secondary dependencies that aren't the focus of this integration test
        patch("app.repos.memory.search_memories", new_callable=AsyncMock, return_value=[]),
        patch("app.handlers.ai_chat.update_stage", new_callable=AsyncMock),
        patch("app.metrics.role_conv_metrics.record_summarization", new_callable=AsyncMock),
    ):
        # Default happy-path setup
        m_resolve.return_value = (
            {"api_key": "k", "key_hash": "h"},
            "gemini-3.1-flash-lite",
            "direct",
        )

        delivery = MagicMock()
        delivery.stream = AsyncMock(
            return_value=CompleteDelivery(
                content_text="Hello world!",
                displayed_text="Hello world!",
                completion=_completion(),
                voice_requested=False,
                receipt=_receipt(),
            )
        )
        get_delivery.return_value = delivery
        yield {
            "resolve": m_resolve,
            "update_chat": m_update_chat,
            "delivery": delivery,
        }


@pytest.mark.asyncio
async def test_successful_chat_response_appended_to_history(mock_boundaries):
    """
    Risk Covered: System fails to persist AI reply or token counts.
    Level: Unit.
    """
    # ── Arrange ──
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    placeholder.chat.type = "private"
    placeholder.get_bot = MagicMock(return_value=None)

    # Pre-existing chat state
    chat_state = make_chat_state(history=[{"role": "user", "parts": ["Hi"]}])

    # ── Act ──
    await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # ── Assert ──
    mock_boundaries["update_chat"].assert_awaited()

    # We assert on the LAST call to update_chat (which finalizes the state)
    saved_state = mock_boundaries["update_chat"].call_args_list[-1][0][1]

    # Verify Behavior: The generated response is appended to history
    assert len(saved_state.history) == 2, "Expected 1 new message in history"
    assert saved_state.history[-1]["role"] == "model"
    assert "Hello world!" in saved_state.history[-1]["parts"][0]

    # Verify Behavior: Token limit correctly updated internally
    assert saved_state.token_count > 0, "Expected updated token count based on the assembled chunk"


@pytest.mark.asyncio
async def test_streamed_chat_delegates_complete_keyboard_to_delivery(mock_boundaries):
    """Delivery owns the final keyboard so a long-read row cannot be overwritten."""
    placeholder = make_telegram_message(user_id=123)
    placeholder.chat.type = "private"
    placeholder.get_bot = MagicMock(return_value=None)
    chat_state = make_chat_state(history=[{"role": "user", "parts": ["Hi"]}])

    await _handle_regular_chat(placeholder, 123, "Hi", chat_state)

    delivery = mock_boundaries["delivery"]
    presentation = delivery.stream.await_args.kwargs["presentation"]
    prepared = presentation.prepare(
        PresentationFacts(
            raw_content="Hello world!\n[INTENT:research]\n[SUGGESTIONS: More details]",
            terminal=_completion(),
            voice_requested=False,
        )
    )

    assert prepared.content_text == "Hello world!"
    assert prepared.actions is not None
    callback_data = [
        button.callback_data
        for row in prepared.actions.inline_keyboard
        for button in row
    ]
    assert any(value and value.startswith("suggest:") for value in callback_data)
    assert "intent_route:research" in callback_data
    assert "retry_last" in callback_data


def test_chat_response_markup_preserves_all_dynamic_actions():
    markup = _build_chat_response_markup(
        "```python\nprint('a sufficiently long code sample')\n```",
        intent="draw",
        suggestions=[{"id": "detail", "label": "Подробнее"}],
        lang="ru",
        branch_id="branch-1",
        is_deep_dive=True,
        is_forward_batch=True,
        memories_injected=2,
        graph_triples_count=3,
    )

    buttons = [button for row in markup.inline_keyboard for button in row]
    callback_data = [button.callback_data for button in buttons]
    assert "suggest:detail" in callback_data
    assert "intent_route:draw" in callback_data
    assert "branch_return" in callback_data
    assert "deepdive:new_topic" in callback_data
    assert "fwd_save" in callback_data
    assert "show_facts" in callback_data
    assert "feedback:reveal" in callback_data
    assert any(button.copy_text is not None for button in buttons)
    assert any(button.text == "📚 5 фактов" for button in buttons)


@pytest.mark.asyncio
async def test_interrupted_chat_delegates_recovery_keyboard_to_stream(mock_boundaries):
    """Recovery actions must be merged by the stream instead of replacing Reader."""
    placeholder = make_telegram_message(user_id=123)
    placeholder.chat.type = "private"
    placeholder.reply_to_message = None
    placeholder.get_bot = MagicMock(return_value=None)
    chat_state = make_chat_state(history=[{"role": "user", "parts": ["Hi"]}])
    terminal = StreamFailed(
        code=ErrorCode.TIMEOUT,
        phase=FailurePhase.AFTER_TEXT,
        retry=RetryDisposition.RETRY_LATER,
        key=KeyDisposition.TRANSIENT_FAILURE,
        diagnostic="stream timeout",
    )
    mock_boundaries["delivery"].stream.return_value = PartialDelivery(
        content_text="Partial response",
        displayed_text="Partial response\n\n⚠️ timeout",
        terminal=terminal,
        voice_requested=False,
        receipt=_receipt(),
    )

    with patch("app.handlers.ai_chat.set_error_reaction", new_callable=AsyncMock):
        await _handle_regular_chat(placeholder, 123, "Hi", chat_state)

    presentation = mock_boundaries["delivery"].stream.await_args.kwargs["presentation"]
    interrupted_markup = presentation.recovery_actions
    assert interrupted_markup is not None
    callback_data = [button.callback_data for row in interrupted_markup.inline_keyboard for button in row]
    assert callback_data == ["continue_stream", "retry_last"]


@pytest.mark.asyncio
async def test_exhausted_limits_shows_error_message(mock_boundaries):
    """
    Risk Covered: System crashes or hangs when API keys are exhausted.
    Level: Unit.
    """
    # ── Arrange ──
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    chat_state = make_chat_state()

    # Simulate routing failing to find any keys
    mock_boundaries["resolve"].return_value = (None, None, "all_exhausted")

    # ── Act ──
    await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # ── Assert ──
    placeholder.edit_text.assert_awaited_once()
    edited_text = placeholder.edit_text.call_args[0][0].lower()
    assert "исчерпаны" in edited_text or "лимит" in edited_text
    # Ensure stream process was definitely bypassed
    mock_boundaries["delivery"].stream.assert_not_called()


@pytest.mark.asyncio
async def test_model_exhausted_prompts_fallback_confirmation(mock_boundaries):
    """
    Risk Covered: Silent failure when switching to fallback model instead of asking user.
    Level: Unit.
    """
    # ── Arrange ──
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    chat_state = make_chat_state()
    mock_boundaries["resolve"].return_value = (
        {"api_key": "fixed_key"},
        "gemini-3.1-flash-lite",
        "confirm_fallback",
    )

    # ── Act ──
    await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # ── Assert ──
    placeholder.edit_text.assert_awaited_once()
    call_args, call_kwargs = placeholder.edit_text.call_args
    assert "reply_markup" in call_kwargs, "Expected inline keyboard for fallback confirmation"
    assert "gemini-3.1-flash-lite" in call_args[0], "Expected fallback model name in prompt"
    # Ensure stream was bypassed while we wait for user confirmation
    mock_boundaries["delivery"].stream.assert_not_called()


@pytest.mark.asyncio
async def test_empty_response_rolls_back_history(mock_boundaries):
    """
    Risk Covered: Storing empty AI responses clutters history and causes errors on next turn.
    Level: Unit.
    """
    # ── Arrange ──
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)

    # Starting history has a user message and a model reply
    chat_state = make_chat_state(
        history=[
            {"role": "user", "parts": ["Hi"]},
            {"role": "model", "parts": ["Hello"]},
        ]
    )

    mock_boundaries["delivery"].stream.return_value = FailedDelivery(
        error_code=ErrorCode.EMPTY_RESPONSE,
        displayed_text="Не удалось получить ответ от API.",
        receipt=_receipt(),
    )

    with patch("app.errors.build_retry_and_roles_keyboard", return_value=None):
        # ── Act ──
        await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # ── Assert ──
    # The handler should attempt to rollback the context assembler's injection
    # In earlier behavior the ContextAssembler didn't append the user msg back to the state in place
    # if it failed, but let's check what state was saved. As long as it hasn't stored an empty model text.
    mock_boundaries["update_chat"].assert_awaited()
    # Find the state saved during the error branch
    saved_state = mock_boundaries["update_chat"].call_args_list[-1][0][1]

    # Make sure we didn't save a model role without content, and rolled back the user message
    # Since we started with user -> model, after adding a user message and failing, it should roll back to user -> model
    assert saved_state.history[-1]["role"] == "model", "Should roll back to the previous model message"

    # The delivery boundary already rendered the failure exactly once.
    placeholder.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_triples_do_not_shadow_translation_function(mock_boundaries):
    """
    Regression test for incident 2026-04-05:
    UnboundLocalError: cannot access local variable 't' where it is not associated with a value.

    Root cause: `for t in current_triples` loop in the graph injection block was
    shadowing `from app.i18n import t`, causing the translation function to become
    unreachable for the rest of _handle_regular_chat.

    This test verifies that when LTM returns both memories AND graph_triples, the
    handler completes without crashing and the final state is persisted normally.

    Risk Covered: Memory-recall queries crash silently mid-response with UnboundLocalError.
    Level: Unit.
    """
    # ── Arrange ──
    user_id = 456
    user_message = "Напомни-ка, как зовут мою жену?"
    placeholder = make_telegram_message(user_id=user_id)
    placeholder.chat.type = "private"
    placeholder.get_bot = MagicMock(return_value=None)

    # LTM enabled so the graph injection code path is triggered
    chat_state = make_chat_state(ltm_enabled=True)

    # Simulate LTM returning memories + both current and SUPERSEDED graph triples —
    # exactly the inputs that caused the variable shadowing crash.
    mock_memories = [{"content": "wife: Anna", "similarity": 0.9}]
    mock_graph_triples = [
        "user HAS_WIFE Anna",
        "[SUPERSEDED] user HAS_WIFE Maria",  # temporal triple — also triggers inner loop
    ]

    with (
        patch(
            "app.repos.memory.search_memories_with_graph",
            new_callable=AsyncMock,
            return_value=(mock_memories, mock_graph_triples),
        ),
        patch(
            "app.handlers.chat_logic.format_memories_for_system_prompt",
            return_value="<memories>wife: Anna</memories>",
        ),
    ):
        # ── Act — must complete without raising UnboundLocalError ──
        await _handle_regular_chat(placeholder, user_id, user_message, chat_state)

    # ── Assert ──
    # Handler completed: state was persisted (not crashed before update_user_chat)
    mock_boundaries["update_chat"].assert_awaited()

    # The response was appended to history (not rolled back due to an error)
    saved_state = mock_boundaries["update_chat"].call_args_list[-1][0][1]
    assert saved_state.history[-1]["role"] == "model", (
        "Expected model response in history; crash before buttons would leave no model turn"
    )
