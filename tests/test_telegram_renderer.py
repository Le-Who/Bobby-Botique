"""Interface-level tests for Telegram rendering and Long Read fallback."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.response_delivery.renderer import (
    DeliveryKind,
    RendererProtocolError,
    TelegramBotTransport,
    TelegramDeliveryError,
    TelegramMessageRef,
    TelegramMessageTransport,
    TelegramRenderer,
)


class RecordingTransport:
    def __init__(self, *, edit_failures: int = 0, send_fails: bool = False):
        self.current_ref = TelegramMessageRef(chat_id=10, message_id=1)
        self.edit_failures = edit_failures
        self.send_fails = send_fails
        self.edits: list[dict] = []
        self.sends: list[dict] = []

    async def edit(self, text, *, parse_mode, reply_markup):
        self.edits.append(
            {"text": text, "parse_mode": parse_mode, "reply_markup": reply_markup}
        )
        if self.edit_failures:
            self.edit_failures -= 1
            raise RuntimeError("edit failed")

    async def send(self, text, *, parse_mode, reply_markup):
        self.sends.append(
            {"text": text, "parse_mode": parse_mode, "reply_markup": reply_markup}
        )
        if self.send_fails:
            raise RuntimeError("send failed")
        self.current_ref = TelegramMessageRef(
            chat_id=self.current_ref.chat_id,
            message_id=self.current_ref.message_id + 1,
        )
        return self.current_ref


def _actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Action", callback_data="action")]]
    )


@pytest.mark.asyncio
async def test_short_final_response_is_authoritative_and_contains_actions():
    transport = RecordingTransport()
    renderer = TelegramRenderer(transport, message_limit=4000)
    session = renderer.open()

    await session.append("draft")
    receipt = await session.finalize(
        displayed_text="**Final** answer",
        title="Answer",
        actions=_actions(),
    )

    assert receipt.kind is DeliveryKind.MESSAGE
    assert receipt.final_message == TelegramMessageRef(chat_id=10, message_id=1)
    assert "Final" in transport.edits[-1]["text"]
    assert transport.edits[-1]["reply_markup"].inline_keyboard[0][0].callback_data == "action"


@pytest.mark.asyncio
async def test_reader_storage_success_does_not_publish_public_copy_by_default():
    transport = RecordingTransport()
    actions = _actions()
    original_rows = actions.inline_keyboard
    submitted = []
    renderer = TelegramRenderer(
        transport,
        message_limit=100,
        webapp_base_url="https://bot.example.com",
        store_long_message=AsyncMock(return_value=True),
        create_telegraph_page=AsyncMock(return_value="https://telegra.ph/cold-copy"),
        store_telegraph_url=AsyncMock(return_value=True),
        submit_background=lambda coroutine: (submitted.append(coroutine), coroutine.close()),
    )

    receipt = await renderer.open().finalize(
        displayed_text="A" * 300,
        title="Answer",
        actions=actions,
    )

    assert receipt.kind is DeliveryKind.READER
    assert receipt.publication_url.startswith("https://bot.example.com/webapp/reader?id=")
    rows = transport.edits[-1]["reply_markup"].inline_keyboard
    assert rows[0][0].web_app.url == receipt.publication_url
    assert rows[1][0].callback_data == "action"
    assert actions.inline_keyboard == original_rows
    assert submitted == []


@pytest.mark.asyncio
async def test_reader_can_publish_telegraph_copy_when_explicitly_enabled():
    transport = RecordingTransport()
    submitted = []
    renderer = TelegramRenderer(
        transport,
        message_limit=100,
        webapp_base_url="https://bot.example.com",
        store_long_message=AsyncMock(return_value=True),
        create_telegraph_page=AsyncMock(return_value="https://telegra.ph/cold-copy"),
        store_telegraph_url=AsyncMock(return_value=True),
        submit_background=lambda coroutine: (submitted.append(coroutine), coroutine.close()),
        telegraph_publication_enabled=True,
    )

    receipt = await renderer.open().finalize(
        displayed_text="A" * 300,
        title="Answer",
        actions=_actions(),
    )

    assert receipt.kind is DeliveryKind.READER
    assert len(submitted) == 1


@pytest.mark.asyncio
async def test_reader_edit_failure_recovers_by_sending_new_message():
    transport = RecordingTransport(edit_failures=1)
    renderer = TelegramRenderer(
        transport,
        message_limit=100,
        webapp_base_url="https://bot.example.com",
        store_long_message=AsyncMock(return_value=True),
        create_telegraph_page=AsyncMock(return_value=None),
        store_telegraph_url=AsyncMock(return_value=False),
        submit_background=lambda coroutine: coroutine.close(),
    )

    receipt = await renderer.open().finalize(
        displayed_text="A" * 300,
        title="Answer",
        actions=_actions(),
    )

    assert receipt.kind is DeliveryKind.READER
    assert receipt.final_message.message_id == 2
    assert len(transport.edits) == 1
    assert len(transport.sends) == 1


@pytest.mark.asyncio
async def test_redis_failure_uses_synchronous_telegraph_fallback():
    transport = RecordingTransport()
    create_page = AsyncMock(return_value="https://telegra.ph/full-answer")
    renderer = TelegramRenderer(
        transport,
        message_limit=100,
        webapp_base_url="https://bot.example.com",
        telegraph_publication_enabled=True,
        store_long_message=AsyncMock(return_value=False),
        create_telegraph_page=create_page,
        store_telegraph_url=AsyncMock(),
    )

    receipt = await renderer.open().finalize(
        displayed_text="A" * 300,
        title="Answer",
        actions=_actions(),
    )

    assert receipt.kind is DeliveryKind.TELEGRAPH
    assert receipt.publication_url == "https://telegra.ph/full-answer"
    assert transport.edits[-1]["reply_markup"].inline_keyboard[0][0].url == receipt.publication_url
    create_page.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegraph_failure_splits_full_answer_and_actions_only_on_last_message():
    transport = RecordingTransport()
    renderer = TelegramRenderer(
        transport,
        message_limit=100,
        webapp_base_url="",
        create_telegraph_page=AsyncMock(return_value=None),
    )

    receipt = await renderer.open().finalize(
        displayed_text=("paragraph " * 80),
        title="Answer",
        actions=_actions(),
    )

    assert receipt.kind is DeliveryKind.SPLIT
    calls = [*transport.edits, *transport.sends]
    assert len(calls) > 1
    assert all(call["reply_markup"] is None for call in calls[:-1])
    assert calls[-1]["reply_markup"].inline_keyboard[0][0].callback_data == "action"


@pytest.mark.asyncio
async def test_total_telegram_failure_raises_delivery_error():
    transport = RecordingTransport(edit_failures=20, send_fails=True)
    renderer = TelegramRenderer(
        transport,
        message_limit=100,
        webapp_base_url="",
        create_telegraph_page=AsyncMock(return_value=None),
    )

    with pytest.raises(TelegramDeliveryError):
        await renderer.open().finalize(
            displayed_text="A" * 300,
            title="Answer",
            actions=_actions(),
        )


@pytest.mark.asyncio
async def test_renderer_session_rejects_append_or_second_finalize_after_finalization():
    session = TelegramRenderer(RecordingTransport()).open()
    await session.finalize(displayed_text="done", title="Answer", actions=None)

    with pytest.raises(RendererProtocolError):
        await session.append("late")
    with pytest.raises(RendererProtocolError):
        await session.finalize(displayed_text="again", title="Answer", actions=None)


@pytest.mark.asyncio
async def test_send_only_transport_supports_deferred_delivery_without_placeholder():
    message = MagicMock(chat_id=10, message_id=77, message_thread_id=None)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=message)
    renderer = TelegramRenderer(TelegramBotTransport(bot, chat_id=10))

    receipt = await renderer.open().finalize(
        displayed_text="Deferred answer",
        title="Answer",
        actions=None,
    )

    assert receipt.final_message == TelegramMessageRef(chat_id=10, message_id=77)
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_final_edit_retries_telegram_flood_control_before_send_recovery():
    class FloodTransport(RecordingTransport):
        async def edit(self, text, *, parse_mode, reply_markup):
            self.edits.append(
                {"text": text, "parse_mode": parse_mode, "reply_markup": reply_markup}
            )
            if len(self.edits) < 3:
                raise RuntimeError("429 Too Many Requests retry_after=1")

    transport = FloodTransport()
    renderer = TelegramRenderer(transport)

    with patch("app.response_delivery.renderer.asyncio.sleep", new_callable=AsyncMock):
        receipt = await renderer.open().finalize(
            displayed_text="answer",
            title="Answer",
            actions=None,
        )

    assert receipt.kind is DeliveryKind.MESSAGE
    assert len(transport.edits) == 3
    assert transport.sends == []


@pytest.mark.asyncio
async def test_long_read_threshold_is_based_on_formatted_html_length():
    transport = RecordingTransport()
    renderer = TelegramRenderer(
        transport,
        message_limit=20,
        webapp_base_url="",
        create_telegraph_page=AsyncMock(return_value=None),
    )

    # Raw Markdown is 19 chars, but Telegram HTML is 22 chars (<b>...</b>).
    receipt = await renderer.open().finalize(
        displayed_text="**" + ("A" * 15) + "**",
        title="Answer",
        actions=None,
    )

    assert receipt.kind is DeliveryKind.SPLIT


@pytest.mark.asyncio
async def test_split_chain_balances_and_reopens_html_formatting():
    transport = RecordingTransport()
    renderer = TelegramRenderer(
        transport,
        message_limit=60,
        webapp_base_url="",
        create_telegraph_page=AsyncMock(return_value=None),
    )

    receipt = await renderer.open().finalize(
        displayed_text="**" + ("word " * 80) + "**",
        title="Answer",
        actions=None,
    )

    assert receipt.kind is DeliveryKind.SPLIT
    chunks = [call["text"] for call in [*transport.edits, *transport.sends]]
    assert len(chunks) > 1
    assert all(chunk.count("<b>") == chunk.count("</b>") for chunk in chunks)


@pytest.mark.asyncio
async def test_message_transport_sends_directly_when_reply_target_was_deleted():
    placeholder = MagicMock(chat_id=10, message_id=1, message_thread_id=77)
    placeholder.chat.id = 10
    placeholder.reply_text = AsyncMock(
        side_effect=RuntimeError("Message to be replied not found")
    )
    sent = MagicMock(chat_id=10, message_id=2, message_thread_id=77)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=sent)
    transport = TelegramMessageTransport(placeholder, bot=bot, chat_id=10)

    ref = await transport.send("answer", parse_mode="HTML", reply_markup=None)

    assert ref == TelegramMessageRef(chat_id=10, message_id=2, message_thread_id=77)
    bot.send_message.assert_awaited_once()
