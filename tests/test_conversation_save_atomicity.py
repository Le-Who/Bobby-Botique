"""Saving a conversation must acknowledge a committed header and transcript together.

The repository and database query implementation are real; only the driver pool
and chat read are synthetic. This exercises transaction ownership, not PostgreSQL
commit durability or the stored procedure's filtering SQL.
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app import database
from app.handlers import cmd_conversations
from app.repos import conversations

pytestmark = pytest.mark.asyncio


class SaveDriver:
    def __init__(self):
        self.headers = {(202, 8): {"title": "Other user's saved chat"}}
        self.messages = {(202, 8): [("user", "Other user's saved turn")]}
        self.source = [("user", "First question"), ("model", "First answer")]
        self.fail_at = None
        self.error = asyncpg.PostgresError("synthetic save failure")
        self.hold_at = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.acquired = 0
        self.released = 0
        self.commits = 0
        self.rollbacks = 0
        self.calls = []
        self._closed = False

    def acquire(self):
        driver = self

        class Acquisition:
            async def __aenter__(self):
                driver.acquired += 1
                return driver

            async def __aexit__(self, *exc):
                driver.released += 1

        return Acquisition()

    def transaction(self):
        driver = self

        class Transaction:
            async def __aenter__(self):
                self.before = deepcopy((driver.headers, driver.messages))

            async def __aexit__(self, exc_type, exc, traceback):
                committed = False
                try:
                    if exc_type is None:
                        await driver.checkpoint("commit")
                        driver.commits += 1
                        committed = True
                        await driver.checkpoint("commit_ack")
                        return False
                except BaseException:
                    if not committed:
                        driver.headers, driver.messages = self.before
                        driver.rollbacks += 1
                    raise
                driver.headers, driver.messages = self.before
                driver.rollbacks += 1
                return False

        return Transaction()

    async def checkpoint(self, phase):
        if self.hold_at == phase:
            self.started.set()
            await self.release.wait()
        if self.fail_at == phase:
            raise self.error

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        if "INSERT INTO public.conversations" in sql:
            await self.checkpoint("insert")
            user_id, title, role_type, role_id, summary, token_budget = params
            self.headers[user_id, 41] = {
                "title": title,
                "role_type": role_type,
                "role_id": role_id,
                "summary": summary,
                "token_budget": token_budget,
            }
            return [{"id": 41}]
        if "CALL save_chat_to_conversation" in sql:
            user_id, conversation_id = params
            # An error after partial driver effects must still roll back the header.
            self.messages[user_id, conversation_id] = list(self.source)
            await self.checkpoint("procedure")
            return []
        raise AssertionError(f"Unexpected save query: {sql}")


@pytest.fixture
def save_driver(monkeypatch):
    driver = SaveDriver()
    chat = SimpleNamespace(history=[{"role": "user", "parts": ["First question"]}], token_count=37, system_prompt=None)
    monkeypatch.setattr(database.db_manager, "pool", driver)
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr(cmd_conversations, "get_user_chat", AsyncMock(return_value=chat))
    return driver, chat


@pytest.mark.parametrize("error_type", [asyncpg.PostgresError, asyncpg.InterfaceError])
async def test_failed_transcript_save_returns_failure_and_restores_existing_rows(save_driver, error_type):
    driver, _ = save_driver
    driver.fail_at = "procedure"
    driver.error = error_type("synthetic transcript failure")
    before = deepcopy((driver.headers, driver.messages))

    assert await conversations.save_conversation(101, "My chat", "user_role", 7) is None

    assert (driver.headers, driver.messages) == before
    assert driver.commits == 0 and driver.rollbacks == 1
    assert driver.acquired == driver.released == 1


@pytest.mark.parametrize("phase", ["insert", "commit"])
@pytest.mark.parametrize("error_type", [asyncpg.PostgresError, asyncpg.InterfaceError])
async def test_save_driver_failure_never_leaves_partial_rows(save_driver, phase, error_type):
    driver, _ = save_driver
    driver.fail_at = phase
    driver.error = error_type("synthetic driver failure")
    before = deepcopy((driver.headers, driver.messages))

    assert await conversations.save_conversation(101, "My chat") is None

    assert (driver.headers, driver.messages) == before
    assert driver.commits == 0 and driver.rollbacks == 1
    assert driver.acquired == driver.released == 1


@pytest.mark.parametrize("phase", ["procedure", "commit"])
async def test_save_cancellation_rolls_back_and_propagates_before_releasing_connection(save_driver, phase):
    driver, _ = save_driver
    driver.hold_at = phase
    before = deepcopy((driver.headers, driver.messages))
    task = asyncio.create_task(conversations.save_conversation(101, "Cancelled save"))
    try:
        await asyncio.wait_for(driver.started.wait(), 1)
        assert not task.done()
        task.cancel("cancel pending save")
        with pytest.raises(asyncio.CancelledError, match="cancel pending save"):
            await task
        assert (driver.headers, driver.messages) == before
        assert driver.commits == 0 and driver.rollbacks == 1
        assert driver.acquired == driver.released == 1
    finally:
        driver.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("history", [True, False], ids=["transcript", "empty"])
async def test_success_returns_id_only_after_commit_and_preserves_owner_and_metadata(save_driver, history):
    driver, chat = save_driver
    if not history:
        chat.history = []
    driver.hold_at = "commit"
    task = asyncio.create_task(conversations.save_conversation(101, "My chat", "user_role", 7))
    try:
        await asyncio.wait_for(driver.started.wait(), 1)
        assert not task.done(), "an uncommitted header must not be acknowledged as saved"
        assert driver.released == 0
        driver.release.set()
        assert await task == 41
        assert driver.headers == {
            (202, 8): {"title": "Other user's saved chat"},
            (101, 41): {
                "title": "My chat",
                "role_type": "user_role",
                "role_id": 7,
                "summary": None,
                "token_budget": 37,
            },
        }
        assert driver.messages == (
            {
                (202, 8): [("user", "Other user's saved turn")],
                (101, 41): [("user", "First question"), ("model", "First answer")],
            }
            if history
            else {(202, 8): [("user", "Other user's saved turn")]}
        )
        assert driver.commits == 1 and driver.rollbacks == 0
        assert driver.acquired == driver.released == 1
    finally:
        driver.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", [None, "procedure", "commit", "commit_ack"])
async def test_save_command_reports_real_repository_outcome(save_driver, monkeypatch, failure):
    driver, _ = save_driver
    driver.fail_at = failure
    if failure == "commit_ack":
        driver.error = asyncpg.InterfaceError("synthetic lost commit acknowledgement")
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=True))
    metric = AsyncMock()
    monkeypatch.setattr(cmd_conversations.role_conv_metrics, "record_conversation_saved", metric)
    update = command_update()

    await cmd_conversations.save_conversation_command(update, SimpleNamespace(args=["My", "chat"], user_data={}))

    if failure:
        update.message.reply_text.assert_awaited_once_with("❌ Ошибка при сохранении беседы")
        metric.assert_not_awaited()
        if failure != "commit_ack":
            assert (101, 41) not in driver.headers and (101, 41) not in driver.messages
        else:
            # A lost COMMIT acknowledgement is ambiguous. Do not blindly repeat
            # the save or claim that already committed rows were rolled back.
            assert (101, 41) in driver.headers and (101, 41) in driver.messages
            assert driver.commits == 1 and driver.rollbacks == 0
    else:
        update.message.reply_text.assert_awaited_once_with("✅ Беседа сохранена с ID: 41")
        metric.assert_awaited_once_with()
        assert driver.commits == 1
    assert driver.acquired == driver.released == 1


def command_update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=101),
        effective_chat=SimpleNamespace(id=101, type="private"),
        callback_query=None,
        message=SimpleNamespace(reply_text=AsyncMock(), text="/save My chat"),
    )


async def test_command_does_not_announce_or_count_save_before_transaction_exit(save_driver, monkeypatch):
    driver, _ = save_driver
    driver.hold_at = "commit"
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=True))
    metric = AsyncMock()
    monkeypatch.setattr(cmd_conversations.role_conv_metrics, "record_conversation_saved", metric)
    update = command_update()
    task = asyncio.create_task(
        cmd_conversations.save_conversation_command(update, SimpleNamespace(args=["My", "chat"], user_data={}))
    )
    try:
        await asyncio.wait_for(driver.started.wait(), 1)
        assert not task.done()
        update.message.reply_text.assert_not_awaited()
        metric.assert_not_awaited()
        driver.release.set()
        await task
        update.message.reply_text.assert_awaited_once_with("✅ Беседа сохранена с ID: 41")
        metric.assert_awaited_once_with()
    finally:
        driver.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
