"""Real duplicate-response -> callback -> upload flow with bounded Telegram edges."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import state
from app.handlers import cb_documents, msg_document
from app.i18n import t


@pytest.fixture
def flow(monkeypatch, tmp_path):
    # Document parsing/persistence are exercised in test_document_upload_atomicity;
    # this boundary exposes whether a duplicate bypass was actually requested.
    attempted_bypasses = []

    async def process(_path, _filename, _user_id, is_path=False, **options):
        attempted_bypasses.append(options.get("skip_duplicate_check", False))
        if options.get("skip_duplicate_check"):
            return {"success": True, "pages": 1, "content": "small document", "text_length": 14}
        return {"error": "duplicate", "duplicate_info": {"id": 5, "filename": "saved.pdf", "created_at": "2026-01-01"}}

    async def download(custom_path):
        from pathlib import Path

        Path(custom_path).write_bytes(b"%PDF synthetic content")

    local_path = tmp_path / "shared.pdf"
    local_path.write_bytes(b"%PDF synthetic content")
    bot_file = SimpleNamespace(file_path=str(local_path), download_to_drive=download)
    document = SimpleNamespace(
        file_name="copy.pdf",
        file_unique_id="telegram-unique-A",
        file_size=32,
        get_file=AsyncMock(return_value=bot_file),
    )
    processing_message = SimpleNamespace(message_id=55, chat_id=101, edit_text=AsyncMock())
    message = SimpleNamespace(document=document, reply_text=AsyncMock(return_value=processing_message))
    query = SimpleNamespace(
        data="doc:force_upload",
        from_user=SimpleNamespace(id=101),
        message=processing_message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(user_data={}, bot=SimpleNamespace(local_mode=False))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=101), message=message)
    monkeypatch.setattr(msg_document, "process_uploaded_document", process)
    monkeypatch.setattr(state, "_schedule_persist", lambda _state: None)
    from app.document_processor import document_processor

    monkeypatch.setattr(
        document_processor,
        "get_user_document_stats",
        AsyncMock(return_value={"document_count": 2, "limit_reached": False}),
    )
    monkeypatch.setattr(msg_document, "get_user_chat", AsyncMock(return_value=SimpleNamespace(ltm_enabled=False)))
    monkeypatch.setattr(msg_document.metrics_collector, "record_api_call", AsyncMock())
    monkeypatch.setattr(msg_document.metrics_collector, "record_error", AsyncMock())
    monkeypatch.setattr(
        cb_documents.menus, "get_documents_menu_content", AsyncMock(return_value=("Documents", None, None))
    )
    monkeypatch.setattr(cb_documents, "get_document_by_id", AsyncMock(return_value=None))
    return SimpleNamespace(
        context=context,
        update=update,
        query=query,
        callback_update=SimpleNamespace(callback_query=query),
        document=document,
        processing_message=processing_message,
        bypasses=attempted_bypasses,
    )


async def show_duplicate_and_confirm(flow):
    await msg_document.handle_document(flow.update, flow.context)
    await cb_documents.document_callback(flow.callback_update, flow.context)


@pytest.mark.asyncio
@pytest.mark.parametrize("local_mode", [False, True])
async def test_oversized_document_is_rejected_before_download(flow, local_mode):
    flow.context.bot.local_mode = local_mode
    flow.document.file_size = 11 * 1024 * 1024
    await msg_document.handle_document(flow.update, flow.context)
    flow.document.get_file.assert_not_awaited()
    assert "10" in flow.update.message.reply_text.await_args.args[0]
    assert flow.bypasses == []


@pytest.mark.asyncio
async def test_truncated_document_discloses_partial_context(flow, monkeypatch):
    monkeypatch.setattr(
        msg_document,
        "process_uploaded_document",
        AsyncMock(
            return_value={"success": True, "pages": 1, "content": "retained", "text_length": 8, "truncated": True}
        ),
    )
    await msg_document.handle_document(flow.update, flow.context)
    assert t("doc.text_truncated").strip() in flow.processing_message.edit_text.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("local_mode", [False, True])
async def test_confirmed_duplicate_upload_bypasses_once_for_matching_file(flow, local_mode):
    flow.context.bot.local_mode = local_mode
    await show_duplicate_and_confirm(flow)
    await msg_document.handle_document(flow.update, flow.context)
    await msg_document.handle_document(flow.update, flow.context)

    assert flow.bypasses == [False, True, False]
    assert state.is_in_document_mode(101) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_confirmation", ["crafted", "stale_message", "other_user", "expired", "repeat"])
async def test_unmatched_duplicate_confirmation_never_bypasses(flow, monkeypatch, invalid_confirmation):
    if invalid_confirmation != "crafted":
        await msg_document.handle_document(flow.update, flow.context)
    if invalid_confirmation == "stale_message":
        flow.query.message = SimpleNamespace(message_id=999, chat_id=101)
    elif invalid_confirmation == "other_user":
        flow.query.from_user.id = 202
    elif invalid_confirmation == "expired":
        actual_monotonic = time.monotonic
        monkeypatch.setattr(msg_document, "monotonic", lambda: actual_monotonic() + 3600, raising=False)
    await cb_documents.document_callback(flow.callback_update, flow.context)
    if invalid_confirmation == "repeat":
        await cb_documents.document_callback(flow.callback_update, flow.context)
    await msg_document.handle_document(flow.update, flow.context)

    assert not any(flow.bypasses)
    assert state.is_in_document_mode(101) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["missing_created_at", "invalid_created_at", "missing_confirmed", "missing_file"])
async def test_malformed_pending_upload_is_discarded_without_bypass(flow, malformed):
    await msg_document.handle_document(flow.update, flow.context)
    pending = flow.context.user_data[msg_document._DUPLICATE_UPLOAD_KEY]
    if malformed == "missing_created_at":
        pending.pop("created_at")
    elif malformed == "invalid_created_at":
        pending["created_at"] = "stale"
    elif malformed == "missing_confirmed":
        pending.pop("confirmed")
    else:
        pending.pop("file_unique_id")

    await cb_documents.document_callback(flow.callback_update, flow.context)
    await msg_document.handle_document(flow.update, flow.context)

    assert flow.bypasses == [False, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["file", "user", "expired"])
async def test_force_intent_is_bound_to_file_owner_and_expiry(flow, monkeypatch, mismatch):
    await show_duplicate_and_confirm(flow)
    if mismatch == "file":
        flow.document.file_unique_id = "telegram-unique-B"
    elif mismatch == "user":
        flow.update.effective_user.id = 202
    else:
        actual_monotonic = time.monotonic
        monkeypatch.setattr(msg_document, "monotonic", lambda: actual_monotonic() + 3600, raising=False)
    await msg_document.handle_document(flow.update, flow.context)

    assert flow.bypasses == [False, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("navigation", ["cancel", "list", "upload_new", "select_document", "use_existing:5"])
async def test_document_navigation_clears_pending_force_intent(flow, navigation):
    await show_duplicate_and_confirm(flow)
    flow.query.data = f"doc:{navigation}"
    await cb_documents.document_callback(flow.callback_update, flow.context)
    await msg_document.handle_document(flow.update, flow.context)

    assert flow.bypasses == [False, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["command", "menu_callback"])
async def test_documents_entrypoints_clear_confirmed_force_intent(flow, monkeypatch, entrypoint):
    from app.handlers import cb_navigation, commands
    from app.utils import decorators

    await show_duplicate_and_confirm(flow)
    if entrypoint == "command":
        monkeypatch.setattr(decorators, "is_authorized", AsyncMock(return_value=True))
        flow.update.callback_query = None
        flow.update.effective_chat = SimpleNamespace(id=101)
        flow.update.message.text = "/documents"
        await commands.documents_command(flow.update, flow.context)
    else:
        flow.query.data = "open_documents"
        await cb_navigation.open_documents_callback(flow.callback_update, flow.context)
    await msg_document.handle_document(flow.update, flow.context)

    assert flow.bypasses == [False, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error_result", "raised_error"])
async def test_upload_storage_failure_has_no_success_mode_or_memory_effect(flow, monkeypatch, failure):
    from app.repos import memory_autosave

    memory_jobs = []
    monkeypatch.setattr(memory_autosave, "submit_memory_task", lambda *args, **kwargs: memory_jobs.append(args))
    if failure == "error_result":
        process = AsyncMock(return_value={"error": "storage_error"})
    else:
        process = AsyncMock(side_effect=RuntimeError("synthetic storage failure"))
    monkeypatch.setattr(msg_document, "process_uploaded_document", process)
    await msg_document.handle_document(flow.update, flow.context)

    assert state.is_in_document_mode(101) is False
    assert memory_jobs == []
    assert flow.processing_message.edit_text.await_count == 1
    assert flow.processing_message.edit_text.await_args.args[0] in {t("doc.process_failed"), t("doc.error_processing")}
    msg_document.get_user_chat.assert_not_awaited()
