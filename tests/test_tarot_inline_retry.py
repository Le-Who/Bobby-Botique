"""
RED tests for _generate_tarot_inline retry/key-rotation behaviour.

Root cause: _generate_tarot_inline used a bespoke single-key call that
re-raised 503 errors directly. It was patched to use _stream_inline_fast,
and then patched again to use ProviderRouter.get_response() to avoid
unnecessary parallel racing for a stable QNA_MODEL.

These tests verify that:
1. Simple inline spreads use only the flash-lite model, while complex spreads
   can use the primary model and its hot standby.
2. On total failure (empty or error message), it gracefully edits the message.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_bot(edit_calls: list | None = None) -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=lambda **kw: edit_calls.append(kw) if edit_calls is not None else None)
    return bot


@pytest.mark.asyncio
@pytest.mark.parametrize("spread_type", ["tarot", "tarot_daily", "tarot_yesno"])
async def test_generate_tarot_inline_uses_only_flash_lite_for_simple_spreads(spread_type):
    """One- and three-card inline spreads must not consume a 3.5 Flash request."""
    from app.handlers.inline import _generate_tarot_inline

    bot = _make_bot()
    mock_router = AsyncMock()
    mock_router.get_response.return_value = ("Карты говорят: всё будет хорошо.", 100)

    with (
        patch("app.providers.router.get_provider_router", return_value=mock_router),
        patch("app.tarot_daily.get_prepared_daily_reading", new=AsyncMock(return_value=None)),
        patch("app.tarot_daily.upsert_prepared_daily_reading", new=AsyncMock()),
    ):
        await _generate_tarot_inline(
            bot=bot,
            inline_message_id="test-simple-spread",
            user_query="таро будем ли мы спорить?",
            user_id=123,
            spread_type=spread_type,
        )

    mock_router.get_response.assert_awaited_once()
    call = mock_router.get_response.await_args.kwargs
    assert call["preferred_model"] == "gemini-3.1-flash-lite"
    assert call["max_key_retries"] == 3
    assert call["use_openrouter"] is False
    assert "❌" not in bot.edit_message_text.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_generate_tarot_inline_shows_error_on_total_failure():
    """When get_response returns (None, 0) or an error message,
    _generate_tarot_inline must gracefully edit the message with
    an error text and NOT raise an unhandled exception.
    """
    from app.handlers.inline import _generate_tarot_inline

    bot = _make_bot()

    mock_router = AsyncMock()
    mock_router.get_response.return_value = (None, 0)

    with patch("app.providers.router.get_provider_router", return_value=mock_router):
        await _generate_tarot_inline(
            bot=bot,
            inline_message_id="test-msg-3",
            user_query="таро любовь",
            user_id=456,
            spread_type="tarot_love",
        )

    bot.edit_message_text.assert_called()
    final_kwargs = bot.edit_message_text.call_args_list[-1][1]
    error_text = final_kwargs.get("text", "")
    assert "❌" in error_text
    assert "Повтор" in error_text
    assert "друг" in error_text.lower()
    retry_markup = final_kwargs.get("reply_markup")
    assert retry_markup is not None
    retry_button = retry_markup.inline_keyboard[0][0]
    assert retry_button.callback_data.startswith("inl_retry:")


@pytest.mark.asyncio
async def test_generate_tarot_inline_shows_error_on_error_message():
    """When get_response returns an error string (e.g. from ProviderRouter),
    it should also show an error gracefully.
    """
    from app.handlers.inline import _generate_tarot_inline

    bot = _make_bot()

    mock_router = AsyncMock()
    # is_error_message checks for certain strings, like "Внутренняя ошибка"
    mock_router.get_response.return_value = ("❌ Ошибка при обращении к API", 0)

    with patch("app.providers.router.get_provider_router", return_value=mock_router), \
         patch("app.handlers.inline.is_error_message", return_value=True):
        await _generate_tarot_inline(
            bot=bot,
            inline_message_id="test-msg-4",
            user_query="таро",
            user_id=456,
            spread_type="tarot",
        )

    bot.edit_message_text.assert_called()
    error_text = bot.edit_message_text.call_args_list[-1][1].get("text", "")
    assert "❌" in error_text


@pytest.mark.asyncio
async def test_generate_tarot_inline_shows_retry_on_provider_exception():
    """Provider exceptions must not leave the inline placeholder stuck forever."""
    from app.handlers.inline import _generate_tarot_inline

    bot = _make_bot()

    mock_router = AsyncMock()
    mock_router.get_response.side_effect = RuntimeError("provider unavailable")

    with patch("app.providers.router.get_provider_router", return_value=mock_router):
        await _generate_tarot_inline(
            bot=bot,
            inline_message_id="test-msg-5",
            user_query="таро",
            user_id=456,
            spread_type="tarot",
        )

    bot.edit_message_text.assert_called()
    final_kwargs = bot.edit_message_text.call_args_list[-1][1]
    error_text = final_kwargs.get("text", "")
    assert "❌" in error_text
    assert "Повтор" in error_text
    retry_markup = final_kwargs.get("reply_markup")
    assert retry_markup is not None
    assert retry_markup.inline_keyboard[0][0].callback_data.startswith("inl_retry:")


@pytest.mark.asyncio
@pytest.mark.parametrize("spread_name", ["LOVE", "CELTIC"])
async def test_generate_tarot_response_prefers_complex_primary_within_deadline(spread_name):
    from app.handlers import inline
    from app.tarot import SpreadType

    calls: list[str] = []
    lite_cancelled = asyncio.Event()

    async def get_response(**kwargs):
        model = kwargs["preferred_model"]
        calls.append(model)
        if model == "gemini-3.5-flash":
            await asyncio.sleep(0.01)
            return "strong answer", 100
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            lite_cancelled.set()
            raise
        return "lite answer", 50

    router = MagicMock()
    router.get_response = AsyncMock(side_effect=get_response)

    result = await inline._generate_tarot_response(
        router=router,
        spread=getattr(SpreadType, spread_name),
        history=[{"role": "user", "parts": ["question"]}],
        system_instruction="system",
        user_id=123,
    )

    assert result == "strong answer"
    assert set(calls) == {"gemini-3.5-flash", "gemini-3.1-flash-lite"}
    assert lite_cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("spread_name", ["LOVE", "CELTIC"])
async def test_generate_tarot_response_uses_ready_lite_after_primary_deadline(monkeypatch, spread_name):
    from app.handlers import inline
    from app.tarot import SpreadType

    calls: list[str] = []
    primary_cancelled = asyncio.Event()

    async def get_response(**kwargs):
        model = kwargs["preferred_model"]
        calls.append(model)
        if model == "gemini-3.5-flash":
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                primary_cancelled.set()
                raise
            return "late strong answer", 100
        await asyncio.sleep(0.01)
        return "ready lite answer", 50

    router = MagicMock()
    router.get_response = AsyncMock(side_effect=get_response)
    monkeypatch.setattr(inline, "_TAROT_COMPLEX_PRIMARY_GRACE_S", 0.05)

    started = time.monotonic()
    result = await inline._generate_tarot_response(
        router=router,
        spread=getattr(SpreadType, spread_name),
        history=[{"role": "user", "parts": ["question"]}],
        system_instruction="system",
        user_id=123,
    )
    elapsed = time.monotonic() - started

    assert result == "ready lite answer"
    assert elapsed < 0.25
    assert set(calls) == {"gemini-3.5-flash", "gemini-3.1-flash-lite"}
    assert primary_cancelled.is_set()


@pytest.mark.asyncio
async def test_generate_tarot_response_uses_lite_immediately_when_primary_fails():
    from app.handlers import inline
    from app.tarot import SpreadType

    async def get_response(**kwargs):
        if kwargs["preferred_model"] == "gemini-3.5-flash":
            raise RuntimeError("primary unavailable")
        return "lite after failure", 50

    router = MagicMock()
    router.get_response = AsyncMock(side_effect=get_response)

    result = await inline._generate_tarot_response(
        router=router,
        spread=SpreadType.LOVE,
        history=[{"role": "user", "parts": ["question"]}],
        system_instruction="system",
        user_id=123,
    )

    assert result == "lite after failure"
