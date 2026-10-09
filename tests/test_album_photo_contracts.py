"""Real album handler contracts: identity, ordering, bounded IO and epoch leases."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import ai_photo
from app.response_delivery.outcomes import CompleteDelivery
from app.response_delivery.renderer import DeliveryKind, DeliveryReceipt, TelegramMessageRef
from app.utils.image_utils import TaggedImage


def photo_message(index, *, user_id=71, is_bot=False):
    async def get_file():
        return index

    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, is_bot=is_bot),
        photo=[SimpleNamespace(get_file=get_file)],
        get_bot=lambda: "synthetic-bot",
    )


class AlbumBoundary:
    def __init__(self, monkeypatch, *, current_epoch=9, lease_current=True):
        # pytest creates a fresh loop per test; retain the production semaphore's
        # capacity while clearing the previous test loop binding after owned IO ended.
        monkeypatch.setattr(ai_photo._DL_SEMAPHORE, "_loop", None)
        self.state = SimpleNamespace(memory_epoch=8, _has_persisted_chat=True)
        self.placeholder = SimpleNamespace(
            from_user=SimpleNamespace(id=999, is_bot=True),
            chat=SimpleNamespace(id=71),
            edit_text=AsyncMock(),
        )
        self.reads = []
        self.epochs = []
        self.leases = []
        self.lease_active = False
        self.lease_exited = False
        self.provider_calls = []
        self.delivery_entered = asyncio.Event()
        self.delivery_release = asyncio.Event()

        async def get_chat(user_id):
            self.reads.append(user_id)
            return self.state

        async def ensure(user_id, *, expected_epoch):
            self.epochs.append((user_id, expected_epoch))
            return current_epoch

        @asynccontextmanager
        async def lease(user_id, expected_epoch, **kwargs):
            self.leases.append((user_id, expected_epoch, kwargs))
            self.lease_active = True
            try:
                yield lease_current
            finally:
                self.lease_active = False
                self.lease_exited = True

        async def vision(placeholder, parts, state, **kwargs):
            assert self.lease_active
            self.provider_calls.append((placeholder, parts, state, kwargs))
            self.delivery_entered.set()
            await self.delivery_release.wait()
            assert self.lease_active
            return CompleteDelivery(
                content_text="Album answer",
                displayed_text="Album answer",
                completion=None,
                voice_requested=False,
                receipt=DeliveryReceipt(
                    kind=DeliveryKind.MESSAGE,
                    message_ids=(1,),
                    final_message=TelegramMessageRef(chat_id=71, message_id=1),
                ),
            )

        monkeypatch.setattr(ai_photo, "get_user_chat", get_chat)
        monkeypatch.setattr(ai_photo, "ensure_chat_generation", ensure)
        monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
        monkeypatch.setattr(ai_photo, "_process_ai_vision", vision)
        monkeypatch.setattr(ai_photo, "update_stage", AsyncMock())

    async def run(self, messages, caption="describe these images", *, update_user=71):
        await ai_photo.process_media_group_request(
            self.placeholder,
            SimpleNamespace(effective_user=SimpleNamespace(id=update_user)),
            None,
            messages,
            caption,
        )


def owned_album_tasks(monkeypatch):
    tasks = []
    create_task = asyncio.TaskGroup.create_task

    def track(group, coro, **kwargs):
        task = create_task(group, coro, **kwargs)
        tasks.append((coro.cr_code.co_name, task))
        return task

    monkeypatch.setattr(asyncio.TaskGroup, "create_task", track)
    return tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caption,task_type,is_ocr",
    [
        ("describe these images", "describe", False),
        ("extract text from these images", "ocr", True),
    ],
)
async def test_album_out_of_order_partial_downloads_keep_exact_images_and_lease_through_delivery(
    monkeypatch, caption, task_type, is_ocr
):
    # gather must preserve original order/filter failed images, and delivery must
    # use the human identity rather than the bot-authored placeholder identity.
    boundary = AlbumBoundary(monkeypatch)
    owned = owned_album_tasks(monkeypatch)
    messages = [photo_message(index) for index in range(6)]
    entered = [asyncio.Event() for _ in messages]
    releases = [asyncio.Event() for _ in messages]
    completed = [asyncio.Event() for _ in messages]
    five_active = asyncio.Event()
    active = peak = 0

    async def download(bot, index):
        nonlocal active, peak
        assert bot == "synthetic-bot"
        assert boundary.lease_active
        active += 1
        peak = max(peak, active)
        entered[index].set()
        if active == 5:
            five_active.set()
        try:
            await releases[index].wait()
            if index == 1:
                raise OSError("synthetic failed second image")
            return f"image-{index}".encode()
        finally:
            active -= 1
            completed[index].set()

    monkeypatch.setattr(ai_photo, "get_file_bytes", download)
    task = asyncio.create_task(boundary.run(messages, caption))
    try:
        await asyncio.wait_for(five_active.wait(), 2)
        assert [event.is_set() for event in entered] == [True, True, True, True, True, False]
        for index in (4, 2, 3, 1, 0, 5):
            await asyncio.wait_for(entered[index].wait(), 2)
            releases[index].set()
            await asyncio.wait_for(completed[index].wait(), 2)
        await asyncio.wait_for(boundary.delivery_entered.wait(), 2)
        assert boundary.lease_active
        assert not boundary.lease_exited
        assert boundary.state.memory_epoch == 9
        assert boundary.epochs == [(71, 8)]
        assert boundary.leases == [(71, 9, {"purpose": "conversation:vision-group", "require_ltm": False})]
        placeholder, parts, state, kwargs = boundary.provider_calls[0]
        assert placeholder is boundary.placeholder
        assert state is boundary.state
        assert kwargs == {"user_id": 71, "chat_id": 71, "is_ocr": is_ocr}
        assert isinstance(parts[0], str) and caption in parts[0]
        assert all(isinstance(part, TaggedImage) for part in parts[1:])
        assert [part.data for part in parts[1:]] == [b"image-0", b"image-2", b"image-3", b"image-4", b"image-5"]
        assert [part.task_type for part in parts[1:]] == [task_type] * 5
        assert peak == 5
        boundary.delivery_release.set()
        await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert boundary.lease_exited and not boundary.lease_active
    assert all(task.done() for _name, task in owned)
    assert [name for name, _task in owned].count("_update_progress") == 1


@pytest.mark.asyncio
async def test_album_all_downloads_fail_without_provider_and_progress_is_awaited(monkeypatch):
    # An empty surviving image set must stop before classification/provider work.
    boundary = AlbumBoundary(monkeypatch)
    owned = owned_album_tasks(monkeypatch)

    async def download(_bot, _index):
        raise OSError("synthetic album download failure")

    monkeypatch.setattr(ai_photo, "get_file_bytes", download)
    classify = AsyncMock(side_effect=AssertionError("No classification after all downloads fail"))
    monkeypatch.setattr(ai_photo, "classify_vision_intent", classify)
    await boundary.run([photo_message(index) for index in range(3)])
    assert boundary.provider_calls == []
    assert classify.await_count == 0
    boundary.placeholder.edit_text.assert_awaited_once_with("❌ Не удалось загрузить ни одного изображения из группы.")
    assert all(task.done() for _name, task in owned)
    assert boundary.lease_exited and not boundary.lease_active


@pytest.mark.asyncio
async def test_album_cancel_awaits_all_downloads_and_progress_before_releasing_lease(monkeypatch):
    # Cancelling the parent must finish active/queued downloads and the progress
    # updater before releasing private data ownership or allowing provider work.
    boundary = AlbumBoundary(monkeypatch)
    owned = owned_album_tasks(monkeypatch)
    all_active = asyncio.Event()
    active = 0
    closed = []

    async def download(_bot, index):
        nonlocal active
        active += 1
        if active == 5:
            all_active.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            assert boundary.lease_active
            closed.append(index)
            active -= 1

    monkeypatch.setattr(ai_photo, "get_file_bytes", download)
    task = asyncio.create_task(boundary.run([photo_message(index) for index in range(7)]))
    await asyncio.wait_for(all_active.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(closed) == {0, 1, 2, 3, 4}
    assert active == 0
    assert len(owned) == 8
    assert all(task.done() for _name, task in owned)
    assert next(task for name, task in owned if name == "_update_progress").cancelled()
    assert boundary.provider_calls == []
    assert boundary.lease_exited and not boundary.lease_active


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages,update_user,diagnostic",
    [
        ([photo_message(0), photo_message(1, user_id=72)], 71, "different senders"),
        ([photo_message(0, is_bot=True), photo_message(1, is_bot=True)], 71, "human sender"),
        ([photo_message(0), photo_message(1)], 72, "sender.*update"),
        ([], 71, "authoritative sender"),
    ],
    ids=["mixed-humans", "bot-sender", "wrong-update-identity", "empty"],
)
async def test_album_invalid_identity_rejected_before_chat_or_private_data_access(
    monkeypatch, messages, update_user, diagnostic
):
    # Identity validation must precede the first private chat read, epoch request,
    # download or provider call, including the public album dispatcher.
    boundary = AlbumBoundary(monkeypatch)
    downloads = AsyncMock(side_effect=AssertionError("No download for invalid identity"))
    monkeypatch.setattr(ai_photo, "get_file_bytes", downloads)
    with pytest.raises(ValueError, match=diagnostic):
        await boundary.run(messages, update_user=update_user)
    assert boundary.reads == []
    assert boundary.epochs == []
    assert boundary.leases == []
    assert downloads.await_count == 0
    assert boundary.provider_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current_epoch,lease_current", [(None, True), (9, False)], ids=["stale-generation", "lease-denied"]
)
async def test_album_stale_or_denied_private_lease_prevents_download_and_provider(
    monkeypatch, current_epoch, lease_current
):
    # A stale generation/denied lease must terminate before any external input IO.
    boundary = AlbumBoundary(monkeypatch, current_epoch=current_epoch, lease_current=lease_current)
    downloads = AsyncMock(side_effect=AssertionError("No IO outside current private generation"))
    monkeypatch.setattr(ai_photo, "get_file_bytes", downloads)
    await boundary.run([photo_message(0), photo_message(1)])
    assert boundary.reads == [71]
    assert boundary.epochs == [(71, 8)]
    assert downloads.await_count == 0
    assert boundary.provider_calls == []
    assert boundary.placeholder.edit_text.await_count == 0
    if current_epoch is None:
        assert boundary.leases == []
    else:
        assert boundary.lease_exited
