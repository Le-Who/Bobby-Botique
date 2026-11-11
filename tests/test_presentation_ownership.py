"""Final presentation waits for its predecessor and confirmed storage."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardMarkup

from app.handlers import board_handler, inline
from app.tarot import SpreadType


@pytest.mark.asyncio
async def test_rejected_board_summary_never_publishes_an_active_card(monkeypatch):
    bot = SimpleNamespace(edit_message_text=AsyncMock())
    monkeypatch.setattr(board_handler, "_run_synthesis", AsyncMock(return_value="Synthetic late summary"))
    monkeypatch.setattr("app.repos.boards_repo.update_summary", AsyncMock(return_value=False))

    await board_handler._maybe_synthesize(
        bot,
        {"id": 91, "inline_msg_id": "synthetic-inline", "topic": "Synthetic topic"},
        [],
        1,
        True,
    )

    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_board_close_waits_for_inflight_summary_publication(monkeypatch):
    board = {
        "id": 92,
        "inline_msg_id": "synthetic-inline",
        "topic": "Synthetic topic",
        "creator_id": 811,
        "entries": [{"text": "Synthetic entry"}],
        "last_summary": "Summary",
    }
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []
    closed = False

    async def edit(**kwargs):
        if "(закрыта)" in kwargs["text"]:
            writes.append("closed")
        else:
            entered.set()
            await release.wait()
            writes.append("active")

    async def close(_board_id):
        nonlocal closed
        closed = True
        return True

    monkeypatch.setattr(board_handler, "_run_synthesis", AsyncMock(return_value="Summary"))
    monkeypatch.setattr("app.repos.boards_repo.update_summary", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.boards_repo.get_board_by_inline_msg", AsyncMock(return_value=board))
    monkeypatch.setattr("app.repos.boards_repo.close_board", close)
    bot = SimpleNamespace(edit_message_text=edit)
    query = SimpleNamespace(
        answer=AsyncMock(),
        data="board_close:92",
        inline_message_id=board["inline_msg_id"],
        from_user=SimpleNamespace(id=811),
    )
    synthesis = asyncio.create_task(board_handler._maybe_synthesize(bot, board, board["entries"], 1, True))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        closing = asyncio.create_task(
            board_handler.handle_board_close_callback(SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot))
        )
        for _ in range(5):
            await asyncio.sleep(0)
        closed_before_prior_publication = closed
    finally:
        release.set()
        await asyncio.gather(synthesis, *([closing] if closing is not None else []))
    assert not closed_before_prior_publication
    assert closed
    assert writes == ["active", "closed"]


@pytest.mark.asyncio
async def test_inline_final_edit_waits_for_progress_cancellation_cleanup(monkeypatch):
    started = asyncio.Event()
    retiring = asyncio.Event()
    release = asyncio.Event()
    writes = []

    async def edit(**kwargs):
        if kwargs["text"] == "Synthetic delayed progress":
            started.set()
            try:
                await asyncio.Future()
            finally:
                retiring.set()
                await release.wait()
                writes.append("progress cleanup")
        else:
            writes.append("final edit")

    async def generate(**kwargs):
        await started.wait()
        return "Synthetic final answer", [], "gemini-3.1-flash-lite"

    monkeypatch.setattr(inline, "get_global_setting", AsyncMock(return_value="off"))
    monkeypatch.setattr(inline, "get_inline_model", AsyncMock(return_value="gemini-3.1-flash-lite"))
    monkeypatch.setattr(inline, "_GEN_PROGRESS_AFTER_S", 0)
    monkeypatch.setattr(inline, "_progress_delayed_html", lambda *_args: "Synthetic delayed progress")
    monkeypatch.setattr(inline, "_generate_inline_answer", generate)
    monkeypatch.setattr(inline, "_build_continue_keyboard", AsyncMock(return_value=InlineKeyboardMarkup([])))
    monkeypatch.setattr(inline.metrics_collector, "record_model_response", AsyncMock())
    monkeypatch.setattr(inline.metrics_collector, "record_request", AsyncMock())
    bot = SimpleNamespace(first_name="Synthetic", username="synthetic_bot", edit_message_text=edit)
    work = asyncio.create_task(
        inline._generate_and_edit_inline(bot, "synthetic-inline", "Synthetic question", "neutral", None)
    )
    try:
        await asyncio.wait_for(retiring.wait(), 1)
        assert writes == []
        assert not work.done()
        release.set()
        await work
        assert writes == ["progress cleanup", "final edit"]
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_inline_tarot_drains_standby_cleanup_through_repeated_cancellation(monkeypatch):
    started, retiring, release, retired = (asyncio.Event() for _ in range(4))

    async def generate(**kwargs):
        if kwargs["preferred_model"] == inline._TAROT_PRIMARY_MODEL:
            await started.wait()
            return "Synthetic complete reading", 1
        started.set()
        try:
            await asyncio.Future()
        finally:
            retiring.set()
            await release.wait()
            retired.set()

    monkeypatch.setattr("app.process_policies.resolve_process", AsyncMock(return_value=SimpleNamespace(explicit=False)))
    spread = next(iter(inline._TAROT_COMPLEX_SPREADS))
    assert isinstance(spread, SpreadType)
    work = asyncio.create_task(
        inline._generate_tarot_response(SimpleNamespace(get_response=generate), spread, [], "Synthetic", 1)
    )
    try:
        await asyncio.wait_for(retiring.wait(), 1)
        work.cancel()
        await asyncio.sleep(0)
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done()
        assert not retired.is_set()
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
    assert retired.is_set()
