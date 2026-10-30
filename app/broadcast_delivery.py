"""One journal owner around existing domain senders, without retaining message text."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from telegram.error import BadRequest, Forbidden, RetryAfter

from app.queue_contracts import JobCancelRequested, JobOwnershipLost


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    outcome: str
    reason_code: str
    retryable: bool = False
    message_id: int | None = None


class DeliveryQuarantined(RuntimeError):
    """A helper must not fall back after an ambiguous external transmission."""


class DeliveryOwner:
    """Every Bot send starts and settles its own fenced database attempt.

    Domain helpers keep their formatting, keyboards and definite-media fallback.
    The proxy observes receipts even when a legacy helper catches an exception or
    returns a boolean. It blocks subsequent sends after uncertain transmission.
    No database transaction remains open while the helper or Telegram runs.
    """

    def __init__(self, journal, *, batch_id, recipient_id, token, bot, checkpoint, sleeper=asyncio.sleep):
        self.journal = journal
        self.batch_id = batch_id
        self.recipient_id = recipient_id
        self.token = token
        self.bot = bot
        self.checkpoint = checkpoint
        self.sleeper = sleeper
        self.sequence = 0
        self.receipts: list[int] = []
        self.result: DeliveryResult | None = None
        self.quarantined = False

    def __getattr__(self, name):
        return getattr(self.bot, name)

    async def send_message(self, *args, **kwargs):
        return await self._send("send_message", args, kwargs)

    async def send_photo(self, *args, **kwargs):
        return await self._send("send_photo", args, kwargs)

    async def _persist(self, attempt_id: str, result: DeliveryResult) -> None:
        try:
            confirmed = await self.journal.finish_attempt(attempt_id, self.token, result)
        except Exception, asyncio.CancelledError:
            confirmed = False
        if not confirmed:
            self.quarantined = True
            self.result = DeliveryResult("unknown_delivery", "journal_commit_unconfirmed")
            raise DeliveryQuarantined("journal_commit_unconfirmed")

    async def _send(self, method, args, kwargs):
        for retry in range(2):
            if self.quarantined:
                raise DeliveryQuarantined("delivery_unconfirmed")
            try:
                await self.checkpoint()
            except (JobCancelRequested, JobOwnershipLost) as exc:
                self.result = DeliveryResult("skipped", getattr(exc, "reason_code", "cancel_requested"))
                raise
            self.sequence += 1
            try:
                attempt = await self.journal.start_attempt(self.batch_id, self.recipient_id, self.token, self.sequence)
            except Exception:
                self.result = DeliveryResult("failed", "journal_start_unavailable", True)
                raise
            if not attempt:
                self.quarantined = True
                self.result = DeliveryResult("unknown_delivery", "recipient_ownership_lost")
                raise DeliveryQuarantined("recipient_ownership_lost")
            attempt_id = attempt["attempt_id"]
            try:
                # Subscription, account epoch and queue owner are rechecked for
                # EACH call, including chunks and a bounded RetryAfter retry.
                await self.checkpoint()
            except JobCancelRequested, JobOwnershipLost:
                self.result = DeliveryResult("skipped", "cancel_requested")
                await self._persist(attempt_id, self.result)
                raise
            try:
                response = await getattr(self.bot, method)(*args, **kwargs)
            except RetryAfter as exc:
                wait = exc.retry_after
                seconds = wait.total_seconds() if isinstance(wait, timedelta) else float(wait)
                self.result = DeliveryResult("failed", "telegram_rate_limited", True)
                await self._persist(attempt_id, self.result)
                if retry == 0 and 0 <= seconds <= 60:
                    await self.sleeper(seconds)
                    continue
                raise
            except (BadRequest, Forbidden) as exc:
                from app.games.daily_telegram_delivery import is_unreachable_chat_error

                reason = "recipient_unreachable" if is_unreachable_chat_error(exc) else "telegram_rejected"
                self.result = DeliveryResult("failed", reason)
                await self._persist(attempt_id, self.result)
                raise
            except Exception, asyncio.CancelledError:
                self.quarantined = True
                self.result = DeliveryResult("unknown_delivery", "telegram_send_unconfirmed")
                await self._persist(attempt_id, self.result)
                raise
            message_id = getattr(response, "message_id", None)
            if type(message_id) is not int:
                self.quarantined = True
                self.result = DeliveryResult("unknown_delivery", "telegram_receipt_missing")
                await self._persist(attempt_id, self.result)
                raise DeliveryQuarantined("telegram_receipt_missing")
            self.receipts.append(message_id)
            self.result = DeliveryResult("sent", "telegram_confirmed", message_id=message_id)
            await self._persist(attempt_id, self.result)
            return response
        raise RuntimeError("unreachable retry state")

    async def deliver(self, helper) -> DeliveryResult:
        try:
            await helper(self)
        except JobCancelRequested, JobOwnershipLost:
            if self.result is None:
                self.result = DeliveryResult("skipped", "cancel_requested")
        except Exception, asyncio.CancelledError:
            if self.result is None:
                self.result = DeliveryResult("failed", "preparation_failed", True)
        if self.quarantined:
            return self.result or DeliveryResult("unknown_delivery", "telegram_send_unconfirmed")
        if self.receipts:
            if self.result and self.result.outcome != "sent":
                return DeliveryResult("unknown_delivery", "partial_delivery")
            return DeliveryResult("sent", "telegram_confirmed", message_id=self.receipts[-1])
        return self.result or DeliveryResult("failed", "preparation_failed", True)
