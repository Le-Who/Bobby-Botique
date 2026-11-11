"""One receipt owner for scheduled generation, progress and final delivery."""

import asyncio
from contextlib import asynccontextmanager

from telegram import Message

from app.broadcast_delivery import DeliveryOwner, DeliveryResult
from app.queue_contracts import JobOwnershipLost
from app.repos import scheduled_deliveries as journal
from app.response_delivery.outcomes import CompleteDelivery, PartialDelivery


class ScheduledDeliveryOwner(DeliveryOwner):
    """Reuse broadcast quarantine semantics for edits as well as new messages."""

    def __init__(self, claim, bot):
        async def checkpoint():
            if not await journal.is_current(claim):
                raise JobOwnershipLost("scheduled_owner_stale")

        super().__init__(
            journal,
            batch_id=claim["delivery_id"],
            recipient_id=claim["user_id"],
            token=claim["owner_token"],
            bot=bot,
            checkpoint=checkpoint,
        )
        self.sequence = claim.get("attempt_sequence", 0)
        self.response_outcome = None

    def observe_response(self, outcome):
        self.response_outcome = outcome

    async def _send(self, method, args, kwargs):
        response = await super()._send(method, args, kwargs)
        # Message.edit_text/reply_text call the bound bot. Every replacement
        # Message must retain this owner, including renderer split fallbacks.
        if isinstance(response, Message):
            response.set_bot(self)
        return response

    async def edit_message_text(self, *args, **kwargs):
        return await self._send("edit_message_text", args, kwargs)

    async def send_voice(self, *args, **kwargs):
        return await self._send("send_voice", args, kwargs)

    async def send_document(self, *args, **kwargs):
        return await self._send("send_document", args, kwargs)

    async def edit_message_reply_markup(self, *args, **kwargs):
        # Keyboard-only edits have no content receipt but still require the
        # current source/epoch. They cannot create a second reminder response.
        await self.checkpoint()
        return await self.bot.edit_message_reply_markup(*args, **kwargs)

    async def delete_message(self, *args, **kwargs):
        await self.checkpoint()
        return await self.bot.delete_message(*args, **kwargs)


@asynccontextmanager
async def _heartbeat(claim):
    worker = asyncio.current_task()

    async def beat():
        while True:
            await asyncio.sleep(15)
            try:
                current = await journal.renew(claim)
            except Exception:
                current = False
            if not current:
                if worker is not None and not worker.cancelling():
                    worker.cancel()
                return

    task = asyncio.create_task(beat(), name="scheduled_delivery_heartbeat")
    try:
        yield
    finally:
        task.cancel()

        async def cleanup():
            await asyncio.gather(task, return_exceptions=True)

        _, cancelled = await _complete_cleanup(cleanup())
        if cancelled:
            raise asyncio.CancelledError


async def _complete_cleanup(work):
    """Retain settlement ownership through repeated shutdown cancellation."""
    task = asyncio.create_task(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _deliver_owned(owner, helper):
    """Cancel a request child once, then retain it through all helper cleanup."""
    task = asyncio.create_task(owner.deliver(helper), name="scheduled_delivery_helper")
    cancelled = False
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled = True
        if not task.done() and not task.cancelling():
            task.cancel()

        async def drain():
            try:
                return await task
            except asyncio.CancelledError:
                # Cancellation before the child starts cannot have a transport
                # effect; the journal still decides whether release is safe.
                return owner.result or DeliveryResult("failed", "worker_interrupted", True)

        result, interrupted = await _complete_cleanup(drain())
        cancelled |= interrupted
    return result, cancelled or bool(task.cancelling())


async def run_scheduled_delivery(claim, bot, helper, *, ai_response=False):
    """Lease the captured generation; settle once, after a confirmed outcome.

    Placeholder/progress receipts protect recovery but cannot acknowledge an AI
    reminder. Only its typed final delivery outcome can do that.
    """
    from app.repos.memory_consent import private_data_lease

    owner = ScheduledDeliveryOwner(claim, bot)
    result = DeliveryResult("skipped", "privacy_generation_changed")
    cancelled = False
    try:
        async with (
            _heartbeat(claim),
            private_data_lease(
                claim["user_id"],
                claim["privacy_epoch"],
                purpose="ltm:scheduled_brief" if claim["kind"] == "brief" else "conversation:reminder",
                require_ltm=claim["kind"] == "brief",
            ) as permitted,
        ):
            if permitted:
                result, interrupted = await _deliver_owned(owner, helper)
                cancelled |= interrupted
            elif await journal.is_current(claim):
                # Lease acquisition also fails closed on temporary database
                # errors. A still-current, untouched claim can safely retry.
                result = DeliveryResult("failed", "private_data_lease_unavailable", True)
            worker = asyncio.current_task()
            cancelled |= worker is not None and bool(worker.cancelling())
    except asyncio.CancelledError:
        cancelled = True
        result = owner.result or DeliveryResult("failed", "worker_interrupted", True)
    except Exception:
        result = owner.result or DeliveryResult("failed", "preparation_failed", True)
    if ai_response and result.outcome != "unknown_delivery":
        if isinstance(owner.response_outcome, CompleteDelivery):
            if result.outcome != "sent":
                result = DeliveryResult("unknown_delivery", "final_receipt_unconfirmed")
        elif isinstance(owner.response_outcome, PartialDelivery):
            result = DeliveryResult("unknown_delivery", "partial_ai_response")
        elif owner.response_outcome is not None:
            result = DeliveryResult("failed", "ai_response_failed")
        elif owner.receipts:
            result = DeliveryResult("unknown_delivery", "ai_completion_unconfirmed")
    if cancelled and not owner.receipts and not owner.quarantined:
        released, interrupted = await _complete_cleanup(journal.release_unstarted(claim))
        cancelled |= interrupted
        if released:
            raise asyncio.CancelledError
    confirmed, interrupted = await _complete_cleanup(journal.finish_delivery(claim, result))
    cancelled |= interrupted
    if cancelled:
        raise asyncio.CancelledError
    return confirmed and result.outcome == "sent"
