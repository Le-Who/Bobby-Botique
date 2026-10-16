"""Stored/public Long Reads count as delivered only once their link is visible."""

import asyncio
from functools import partial
from html import unescape
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.response_delivery.delivery import CompletedResponse, TelegramResponseDelivery, TelegramTarget
from app.response_delivery.outcomes import CompleteDelivery
from app.response_delivery.presentation import FixedPresentation
from app.response_delivery.renderer import DeliveryKind, TelegramRenderer
from app.utils.background_tasks import TaskManager

CONTENT = "Начало ответа. " + "A" * 180 + " Конец ответа."
PUBLIC_URL = "https://telegra.ph/synthetic-long-answer"
ACTIONS = InlineKeyboardMarkup([[InlineKeyboardButton("Continue", callback_data="continue_stream")]])


class TelegramMessageIO:
    """Only accepted edits/sends change the messages visible to the user."""

    def __init__(self, *, fail_links=(), fail_reader_edit=False, gate_reader_send=False):
        self.message_id = 101
        self.chat_id = 10
        self.message_thread_id = None
        self.visible = {}
        self.attempts = []
        self.fail_links = set(fail_links)
        self.fail_reader_edit = fail_reader_edit
        self.gate_reader_send = gate_reader_send
        self.reader_send_started = asyncio.Event()
        self.release_reader_send = asyncio.Event()
        self.next_message_id = 102

    @staticmethod
    def _link_kind(kwargs):
        markup = kwargs.get("reply_markup")
        for row in markup.inline_keyboard if markup else ():
            for button in row:
                if button.web_app:
                    return "reader"
                if button.url == PUBLIC_URL:
                    return "telegraph"
        return "text"

    async def _accept(self, method, text, kwargs):
        kind = self._link_kind(kwargs)
        self.attempts.append((method, kind))
        if kind == "reader" and method == "send" and self.gate_reader_send:
            self.reader_send_started.set()
            await self.release_reader_send.wait()
        if kind in self.fail_links or (kind == "reader" and method == "edit" and self.fail_reader_edit):
            raise RuntimeError(f"synthetic {kind} link {method} failure")
        message_id = 101 if method == "edit" else self.next_message_id
        if method == "send":
            self.next_message_id += 1
        self.visible[message_id] = {"text": text, **kwargs}
        return SimpleNamespace(
            chat_id=self.chat_id,
            message_id=message_id,
            message_thread_id=None,
            reply_text=self.reply_text,
        )

    async def edit_text(self, text, **kwargs):
        return await self._accept("edit", text, kwargs)

    async def reply_text(self, text, **kwargs):
        return await self._accept("send", text, kwargs)


class PublicationIO:
    def __init__(self, *, reader_available=True):
        self.reader_available = reader_available
        self.stored = {}
        self.created_pages = []
        self.mirrors = {}

    async def store_reader(self, uid, text):
        if self.reader_available:
            self.stored[uid] = text
        return self.reader_available

    async def create_page(self, title, text):
        self.created_pages.append((title, text))
        return PUBLIC_URL

    async def store_mirror(self, uid, url):
        self.mirrors[uid] = url
        return True


@pytest.fixture
async def long_read():
    manager = TaskManager()

    def build(message, *, publication, reader_available=True):
        io = PublicationIO(reader_available=reader_available)
        delivery = TelegramResponseDelivery(
            renderer_factory=partial(
                TelegramRenderer,
                message_limit=100,
                webapp_base_url="https://bot.example.com",
                telegraph_publication_enabled=publication,
                store_long_message=io.store_reader,
                create_telegraph_page=io.create_page,
                store_telegraph_url=io.store_mirror,
                submit_background=manager.submit,
            )
        )

        async def deliver():
            return await delivery.deliver(
                TelegramTarget(placeholder_message=message),
                CompletedResponse(CONTENT),
                presentation=FixedPresentation(actions=ACTIONS, long_read_title="Synthetic answer"),
            )

        return deliver, io, manager

    try:
        yield build
    finally:
        assert await manager.drain(timeout=1), "Long Read background work must finish inside the test"


def assert_visible_split(outcome, message):
    assert isinstance(outcome, CompleteDelivery)
    assert outcome.content_text == CONTENT
    receipt = outcome.receipt
    assert receipt.kind is DeliveryKind.SPLIT
    assert receipt.publication_url is None
    assert receipt.message_ids == tuple(message.visible)
    assert len(receipt.message_ids) > 1
    assert receipt.final_message.message_id == receipt.message_ids[-1]
    assert receipt.final_message.chat_id == 10
    visible_parts = [message.visible[mid] for mid in receipt.message_ids]
    assert unescape("".join(part["text"] for part in visible_parts)) == CONTENT
    assert all(len(part["text"]) <= 100 for part in visible_parts)
    assert all(part["reply_markup"] is None for part in visible_parts[:-1])
    assert visible_parts[-1]["reply_markup"] == ACTIONS


@pytest.mark.asyncio
@pytest.mark.parametrize("publication", [False, True], ids=["reader-to-split", "reader-and-telegraph-to-split"])
async def test_invisible_reader_link_falls_through_to_full_text_without_mirror(long_read, publication):
    # Returning a Reader receipt after storage alone would silently lose the answer.
    message = TelegramMessageIO(fail_links={"reader", "telegraph"})
    deliver, io, manager = long_read(message, publication=publication)

    outcome = await deliver()
    assert await manager.drain(timeout=1)

    assert_visible_split(outcome, message)
    assert list(io.stored.values()) == [CONTENT]
    expected_attempts = [("edit", "reader"), ("send", "reader")]
    if publication:
        expected_attempts += [("edit", "telegraph"), ("send", "telegraph")]
    assert message.attempts[: len(expected_attempts)] == expected_attempts
    assert message.attempts[len(expected_attempts)] == ("edit", "text")
    assert io.created_pages == ([("Synthetic answer", CONTENT)] if publication else [])
    assert io.mirrors == {}


@pytest.mark.asyncio
async def test_invisible_reader_link_can_recover_with_visible_telegraph_link(long_read):
    message = TelegramMessageIO(fail_links={"reader"})
    deliver, io, manager = long_read(message, publication=True)

    outcome = await deliver()
    assert await manager.drain(timeout=1)

    assert isinstance(outcome, CompleteDelivery)
    assert outcome.content_text == CONTENT
    assert outcome.receipt.kind is DeliveryKind.TELEGRAPH
    assert outcome.receipt.message_ids == (101,)
    assert outcome.receipt.final_message.message_id == 101
    assert outcome.receipt.publication_url == PUBLIC_URL
    assert list(message.visible) == [101]
    visible = message.visible[101]
    assert PUBLIC_URL in visible["text"]
    assert visible["reply_markup"].inline_keyboard[0][0].url == PUBLIC_URL
    assert visible["reply_markup"].inline_keyboard[1:] == ACTIONS.inline_keyboard
    assert message.attempts == [("edit", "reader"), ("send", "reader"), ("edit", "telegraph")]
    assert list(io.stored.values()) == [CONTENT]
    assert io.created_pages == [("Synthetic answer", CONTENT)]
    assert io.mirrors == {}


@pytest.mark.asyncio
async def test_created_telegraph_page_with_invisible_link_still_delivers_full_text(long_read):
    message = TelegramMessageIO(fail_links={"telegraph"})
    deliver, io, manager = long_read(message, publication=True, reader_available=False)

    outcome = await deliver()
    assert await manager.drain(timeout=1)

    assert_visible_split(outcome, message)
    assert message.attempts[:3] == [("edit", "telegraph"), ("send", "telegraph"), ("edit", "text")]
    assert io.stored == {}
    assert io.created_pages == [("Synthetic answer", CONTENT)]
    assert io.mirrors == {}


@pytest.mark.asyncio
async def test_cancelled_reader_link_send_stops_without_fallback_or_cold_mirror(long_read):
    message = TelegramMessageIO(fail_links={"reader"}, gate_reader_send=True)
    deliver, io, manager = long_read(message, publication=True)
    operation = asyncio.create_task(deliver())
    try:
        await asyncio.wait_for(message.reader_send_started.wait(), timeout=1)
        assert await manager.drain(timeout=1)
        assert list(io.stored.values()) == [CONTENT]
        assert io.created_pages == []
        assert io.mirrors == {}
        assert message.visible == {}

        operation.cancel("cancel invisible Reader link")
        with pytest.raises(asyncio.CancelledError, match="cancel invisible Reader link"):
            await operation

        assert await manager.drain(timeout=1)
        assert operation.cancelled()
        assert message.attempts == [("edit", "reader"), ("send", "reader")]
        assert message.visible == {}
        assert io.created_pages == []
        assert io.mirrors == {}
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
async def test_reader_cold_mirror_starts_only_after_its_link_is_visible(long_read):
    message = TelegramMessageIO(fail_reader_edit=True, gate_reader_send=True)
    deliver, io, manager = long_read(message, publication=True)
    operation = asyncio.create_task(deliver())
    try:
        await asyncio.wait_for(message.reader_send_started.wait(), timeout=1)
        assert await manager.drain(timeout=1)
        assert not operation.done()
        assert message.visible == {}
        assert io.created_pages == []
        assert io.mirrors == {}

        message.release_reader_send.set()
        outcome = await operation
        assert await manager.drain(timeout=1)

        assert outcome.receipt.kind is DeliveryKind.READER
        assert outcome.receipt.message_ids == (102,)
        assert outcome.receipt.final_message.message_id == 102
        assert list(message.visible) == [102]
        visible = message.visible[102]
        link = visible["reply_markup"].inline_keyboard[0][0].web_app.url
        assert outcome.receipt.publication_url == link
        assert link in visible["text"]
        assert visible["reply_markup"].inline_keyboard[1:] == ACTIONS.inline_keyboard
        uid = link.split("?id=", 1)[1]
        assert io.stored == {uid: CONTENT}
        assert io.created_pages == [("Synthetic answer", CONTENT)]
        assert io.mirrors == {uid: PUBLIC_URL}
        assert message.attempts == [("edit", "reader"), ("send", "reader")]
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
