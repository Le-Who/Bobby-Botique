"""Real delivery boundaries for private long answers and partial transport failure."""

import asyncio
import re
from contextlib import asynccontextmanager
from functools import partial
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import state, voice_engine
from app.context.token_budget import AssembledContext
from app.errors import ErrorCode
from app.handlers import ai_chat
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GenerationRequest,
    GroundingReport,
    KeyDisposition,
    PromptRole,
    PromptTurn,
    ProviderKind,
    RequestScope,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamFailed,
    TextDelta,
    TextPart,
    TokenUsage,
)
from app.response_delivery.delivery import CompletedResponse, TelegramResponseDelivery, TelegramTarget
from app.response_delivery.outcomes import CompleteDelivery, PartialDelivery
from app.response_delivery.presentation import FixedPresentation
from app.response_delivery.renderer import DeliveryKind, TelegramDeliveryError, TelegramRenderer
from app.utils import heartbeat
from tests.factories import make_chat_state


def completed():
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=42),
        grounding=GroundingReport(),
        route=RouteUsed(ProviderKind.GEMINI, "gemini-3.1-flash-lite", "gemini-3.1-flash-lite"),
    )


class ScriptedRouter:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        try:
            for event in self.events:
                yield event
        finally:
            self.closed = True


class TelegramMessageDouble:
    """Maintain visible messages so progressive edits cannot count as final splits."""

    def __init__(self, *, send_mode="complete", lease_is_active=lambda: True):
        self.message_id = 501
        self.chat_id = 456
        self.message_thread_id = None
        self.reply_to_message = None
        self.chat = SimpleNamespace(id=456, type="private")
        self.visible = {}
        self.sends = []
        self.second_chunk_entered = asyncio.Event()
        self.allow_second_chunk = asyncio.Event()
        self.send_mode = send_mode
        self.lease_is_active = lease_is_active

    def get_bot(self):
        return None

    async def edit_text(self, text, **kwargs):
        self.visible[self.message_id] = {"text": text, **kwargs}
        return self

    async def reply_text(self, **kwargs):
        assert self.lease_is_active(), "Private delivery must retain its request lease"
        self.sends.append(kwargs)
        if len(self.sends) == 1:
            self.second_chunk_entered.set()
            if self.send_mode == "fail":
                raise RuntimeError("synthetic second Telegram chunk failure")
            if self.send_mode == "cancel":
                await self.allow_second_chunk.wait()
        message_id = self.message_id + len(self.sends)
        self.visible[message_id] = kwargs
        return SimpleNamespace(
            chat_id=self.chat_id,
            message_id=message_id,
            message_thread_id=None,
            reply_text=self.reply_text,
            get_bot=self.get_bot,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("publication", ["enabled", "default_disabled"])
@pytest.mark.parametrize("mode", ["completed", "stream_complete", "stream_partial"])
async def test_private_long_delivery_keeps_full_text_in_telegram_only(monkeypatch, publication, mode):
    """Losing either private guard or the facade flag would expose the whole text."""
    from app.config import settings

    monkeypatch.setattr(settings, "TELEGRAPH_PUBLICATION_ENABLED", False)
    store_reader = AsyncMock(return_value=True)
    create_page = AsyncMock(return_value="https://telegra.ph/synthetic-public-copy")
    store_url = AsyncMock(return_value=True)
    submit_background = MagicMock(side_effect=lambda coroutine: coroutine.close())
    renderer_options = {
        "message_limit": 100,
        "webapp_base_url": "https://bot.example.com",
        "store_long_message": store_reader,
        "create_telegraph_page": create_page,
        "store_telegraph_url": store_url,
        "submit_background": submit_background,
    }
    if publication == "enabled":
        renderer_options["telegraph_publication_enabled"] = True
    terminal = completed()
    if mode == "stream_partial":
        terminal = StreamFailed(
            code=ErrorCode.TIMEOUT,
            phase=FailurePhase.AFTER_TEXT,
            retry=RetryDisposition.DO_NOT_RETRY,
            key=KeyDisposition.UNCHANGED,
            diagnostic="synthetic timeout after private text",
        )
    router = ScriptedRouter([TextDelta("A" * 251), terminal])
    delivery = TelegramResponseDelivery(router, renderer_factory=partial(TelegramRenderer, **renderer_options))
    message = TelegramMessageDouble()
    target = TelegramTarget(placeholder_message=message, private_content=True)
    actions = InlineKeyboardMarkup([[InlineKeyboardButton("Action", callback_data="action")]])
    presentation = FixedPresentation(actions=actions, recovery_actions=actions)

    if mode == "completed":
        outcome = await delivery.deliver(target, CompletedResponse("A" * 251), presentation=presentation)
    else:
        outcome = await delivery.stream(
            target,
            GenerationRequest(
                models=("gemini-3.1-flash-lite",),
                turns=(PromptTurn(PromptRole.USER, (TextPart("private question"),)),),
            ),
            presentation=presentation,
        )
        assert router.closed

    assert isinstance(outcome, PartialDelivery if mode == "stream_partial" else CompleteDelivery)
    assert outcome.content_text == "A" * 251
    assert outcome.receipt.kind is DeliveryKind.SPLIT
    assert outcome.receipt.publication_url is None
    messages = [message.visible[mid] for mid in outcome.receipt.message_ids]
    visible_text = unescape(re.sub(r"<[^>]+>", "", "".join(part["text"] for part in messages)))
    expected = "A" * 251
    if mode == "stream_partial":
        expected += "\n\n⚠️ (ответ был прерван по таймауту)"
    assert visible_text == expected
    assert all(part["reply_markup"] is None for part in messages[:-1])
    assert messages[-1]["reply_markup"] == actions
    assert outcome.receipt.final_message.message_id == outcome.receipt.message_ids[-1]
    store_reader.assert_not_awaited()
    create_page.assert_not_awaited()
    store_url.assert_not_awaited()
    submit_background.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("send_mode", ["complete", "fail", "cancel"])
async def test_chat_after_first_split_obeys_history_effects_and_releases_request(monkeypatch, send_mode):
    """A transport interruption must never reach the successful-response side effects."""
    effects = []
    active_leases = []
    lease_events = []

    @asynccontextmanager
    async def tracked_lease(user_id, epoch, *, purpose, require_ltm):
        assert (user_id, epoch, require_ltm) == (123, 7, True)
        active_leases.append(purpose)
        lease_events.append(("enter", purpose))
        try:
            yield True
        finally:
            lease_events.append(("exit", purpose))
            active_leases.remove(purpose)

    original_history = [
        {"role": "user", "parts": ["Old question"]},
        {"role": "model", "parts": ["Old answer"]},
    ]
    question = "New private question whose text memory capture is eligible"
    chat = make_chat_state(
        history=list(original_history), context_summary="durable old summary", memory_epoch=7, token_count=9
    )
    chat.voice_id = None
    chat.tts_temperature = None
    assembled = AssembledContext(
        history=[{"role": "user", "parts": [question]}],
        system_instruction="synthetic system instruction",
        retained_history=[],
        summary="request-local summary",
        was_truncated=True,
        messages_dropped=2,
        audit_hash="synthetic-audit",
        llm_summarization_scheduled=True,
        dropped_messages=original_history,
    )
    assembler = MagicMock()
    assembler.assemble.return_value = assembled
    assembler._extract_text.side_effect = lambda turn: turn["parts"][0]
    router = ScriptedRouter([TextDelta("[VOICE] " + "A" * 251), completed()])
    delivery = TelegramResponseDelivery(
        router,
        renderer_factory=partial(
            TelegramRenderer,
            message_limit=100,
            webapp_base_url="https://bot.example.com",
            telegraph_publication_enabled=True,
        ),
    )
    message = TelegramMessageDouble(send_mode=send_mode, lease_is_active=lambda: "ltm:chat" in active_leases)

    def side_effect(name):
        def record(*args, **kwargs):
            assert active_leases == ["ltm:chat"]
            effects.append((name, args, kwargs))

        return record

    monkeypatch.setattr(ai_chat, "ensure_chat_generation", AsyncMock(return_value=7))
    monkeypatch.setattr(
        ai_chat, "_resolve_ai_request", AsyncMock(return_value=({"api_key": "synthetic"}, chat.model, "direct"))
    )
    monkeypatch.setattr(
        "app.process_policies.resolve_process",
        AsyncMock(return_value=SimpleNamespace(explicit=False, models=(chat.model,), strategy="legacy", revision=0)),
    )
    monkeypatch.setattr("app.context_assembler.get_assembler", lambda: assembler)
    monkeypatch.setattr(ai_chat, "update_stage", AsyncMock())
    monkeypatch.setattr("app.metrics.role_conv_metrics.record_summarization", AsyncMock())
    monkeypatch.setattr(
        "app.context.compression.inject_memory_layers",
        AsyncMock(return_value=("synthetic private prompt", {"l2_graph_triples": 2})),
    )
    monkeypatch.setattr("app.repos.memory.get_current_retrieved_edge_ids", lambda uid: [42, 43])
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", tracked_lease)
    monkeypatch.setattr("app.response_delivery.delivery.get_telegram_response_delivery", lambda: delivery)
    monkeypatch.setattr(ai_chat, "update_user_chat", AsyncMock(side_effect=side_effect("persist")))
    monkeypatch.setattr("app.repos.memory.bind_retrieved_edges_to_response", side_effect("provenance"))
    monkeypatch.setattr(voice_engine, "fire_voice_reply", AsyncMock(side_effect=side_effect("voice")))
    monkeypatch.setattr("app.context.summarizer.schedule_llm_summarization", side_effect("summary"))
    monkeypatch.setattr(ai_chat, "_store_memory_in_background", side_effect("memory"))
    monkeypatch.setattr("app.metrics.metrics_collector.record_api_call", AsyncMock())
    monkeypatch.setattr("app.metrics.metrics_collector.record_request", AsyncMock())
    monkeypatch.setattr("app.model_selector.select_model", lambda *args, **kwargs: None)
    heartbeat_event = asyncio.Event()
    heartbeat.register_heartbeat(message.message_id, heartbeat_event)
    baseline_tasks = asyncio.all_tasks()

    operation = asyncio.create_task(ai_chat._handle_regular_chat(message, 123, question, chat, reply_with_voice=True))
    try:
        if send_mode == "cancel":
            await asyncio.wait_for(message.second_chunk_entered.wait(), timeout=1)
            assert active_leases == ["ltm:chat"]
            assert message.visible[501]["text"] == "A" * 100
            assert effects == []
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        elif send_mode == "fail":
            with pytest.raises(TelegramDeliveryError, match="split failed on part 2"):
                await operation
        else:
            await operation

        assert router.closed
        assert active_leases == []
        assert lease_events == [
            ("enter", "ltm:chat_recall"),
            ("exit", "ltm:chat_recall"),
            ("enter", "ltm:chat"),
            ("exit", "ltm:chat"),
        ]
        assert heartbeat_event.is_set()
        assert not heartbeat.is_heartbeat_active(message.message_id)
        assert 123 not in state._NETWORK_STALL_SINCE
        assert asyncio.all_tasks() <= baseline_tasks
        if send_mode == "complete":
            assert [effect[0] for effect in effects] == ["provenance", "voice", "persist", "summary", "memory"]
            assert chat.history == [
                {"role": "user", "parts": [question]},
                {"role": "model", "parts": ["A" * 251]},
            ]
            assert chat.token_count == 42
            assert effects[0][1] == (123, 503)
            assert effects[0][2] == {"edge_ids": [42, 43]}
            assert effects[1][2]["reply_to_message_id"] == 503
            assert effects[1][2]["expected_epoch"] == 7
            assert effects[2][2] == {"rewrite_history": True, "expected_epoch": 7}
            assert effects[3][2]["dropped_messages"] == original_history
            assert effects[4][2] == {"expected_epoch": 7}
        else:
            assert list(message.visible) == [501]
            assert message.visible[501]["text"] == "A" * 100
            assert message.visible[501]["reply_markup"] is None
            assert len(message.sends) == 1
            assert effects == []
            # The current implementation retains the assembled user turn in
            # process memory, but appends/persists no model turn without a receipt.
            assert chat.history == [{"role": "user", "parts": [question]}]
            assert chat.token_count == 9
        assert chat.context_summary == "request-local summary"
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        heartbeat.unregister_heartbeat(message.message_id)
        state.clear_network_stall(123)
