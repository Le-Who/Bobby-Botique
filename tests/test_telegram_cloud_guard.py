from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.telegram_cloud_guard import release_cloud_bot_api_session


@dataclass
class FakeWebhookInfo:
    url: str = ""
    pending_update_count: int = 0


class FakeCloudBot:
    def __init__(self, infos, *, log_out_error: Exception | None = None):
        self._infos = list(infos)
        self._log_out_error = log_out_error
        self.delete_webhook_calls: list[dict[str, bool]] = []
        self.log_out_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get_webhook_info(self):
        result = self._infos.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def delete_webhook(self, *, drop_pending_updates: bool):
        self.delete_webhook_calls.append({"drop_pending_updates": drop_pending_updates})
        return True

    async def log_out(self):
        self.log_out_calls += 1
        if self._log_out_error is not None:
            raise self._log_out_error
        return True


class FailingEnterCloudBot:
    async def __aenter__(self):
        raise RuntimeError("BadRequest: Logged out")

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_release_cloud_bot_api_deletes_active_webhook_before_logout() -> None:
    bot = FakeCloudBot([FakeWebhookInfo("https://example.test/webhook/token"), FakeWebhookInfo("")])

    result = await release_cloud_bot_api_session("123:test", bot_factory=lambda _token: bot)

    assert result.ok is True
    assert result.webhook_was_active is True
    assert result.delete_webhook_called is True
    assert result.log_out_called is True
    assert result.cloud_webhook_still_active is False
    assert bot.delete_webhook_calls == [{"drop_pending_updates": True}]
    assert bot.log_out_calls == 1


@pytest.mark.asyncio
async def test_release_cloud_bot_api_fails_when_cloud_webhook_survives_release() -> None:
    bot = FakeCloudBot(
        [
            FakeWebhookInfo("https://example.test/webhook/token"),
            FakeWebhookInfo("https://example.test/webhook/token"),
        ]
    )

    result = await release_cloud_bot_api_session("123:test", bot_factory=lambda _token: bot)

    assert result.ok is False
    assert result.cloud_webhook_still_active is True
    assert result.status == "cloud_webhook_still_active"


@pytest.mark.asyncio
async def test_release_cloud_bot_api_treats_already_logged_out_cloud_as_released() -> None:
    bot = FakeCloudBot([RuntimeError("Unauthorized: logged out from the cloud Bot API server")])

    result = await release_cloud_bot_api_session("123:test", bot_factory=lambda _token: bot)

    assert result.ok is True
    assert result.status == "cloud_already_released"
    assert result.delete_webhook_called is False
    assert result.log_out_called is False


@pytest.mark.asyncio
async def test_release_cloud_bot_api_treats_context_enter_logged_out_as_released() -> None:
    result = await release_cloud_bot_api_session("123:test", bot_factory=lambda _token: FailingEnterCloudBot())

    assert result.ok is True
    assert result.status == "cloud_already_released"
