"""An inline image job owns one outcome across provider and Telegram delivery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import metrics
from app.handlers import inline
from app.metrics import MetricsCollector


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "empty", "provider_error", "upload_error", "edit_error", "cancelled"])
async def test_inline_image_counts_its_completed_user_operation(monkeypatch, outcome):
    collector = MetricsCollector()
    monkeypatch.setattr(metrics, "metrics_collector", collector)
    image = SimpleNamespace(
        success=outcome != "empty", images=[] if outcome == "empty" else [b"synthetic image"], error_message="empty"
    )
    generate = AsyncMock(return_value=image)
    if outcome in {"provider_error", "cancelled"}:
        generate.side_effect = (
            ValueError("synthetic provider failure") if outcome == "provider_error" else asyncio.CancelledError
        )
    monkeypatch.setattr(
        "app.providers.pollinations.get_pollinations_provider", lambda: SimpleNamespace(generate=generate)
    )
    uploaded = SimpleNamespace(photo=[SimpleNamespace(file_id="synthetic-file-id")], delete=AsyncMock())
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=uploaded),
        edit_message_media=AsyncMock(),
        edit_message_caption=AsyncMock(),
    )
    if outcome == "upload_error":
        bot.send_photo.side_effect = ValueError("synthetic upload failure")
    elif outcome == "edit_error":
        bot.edit_message_media.side_effect = ValueError("synthetic edit failure")
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await inline._generate_and_swap_media(bot, "inline-id", "synthetic prompt", "zimage", 42)
    else:
        await inline._generate_and_swap_media(bot, "inline-id", "synthetic prompt", "zimage", 42)
    collector._drain_events()
    assert collector.metrics.request_count == int(outcome != "cancelled")
    assert collector.metrics.error_count == int(outcome not in {"completed", "cancelled"})
    assert sum(users.get(42, {}).get("request_count", 0) for users in collector._user_daily.values()) == int(
        outcome != "cancelled"
    )
