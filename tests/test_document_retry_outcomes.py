"""Only proven transient failures before storage starts permit an upload retry."""

from unittest.mock import AsyncMock

import asyncpg
import pytest

from app import document_processor as processor
from app.documents import repository
from app.queue import TaskQueue
from app.queue_contracts import JobOutcome


@pytest.mark.parametrize(
    "error_type, retryable",
    [
        (asyncpg.CannotConnectNowError, True),
        (asyncpg.ConnectionDoesNotExistError, True),
        (asyncpg.TooManyConnectionsError, True),
        (asyncpg.AdminShutdownError, True),
        (TimeoutError, True),
        (ConnectionError, True),
        (asyncpg.PostgresError, False),
        (asyncpg.UndefinedTableError, False),
        (asyncpg.InsufficientPrivilegeError, False),
        (asyncpg.InterfaceError, False),
    ],
)
async def test_initial_duplicate_lookup_outage_retries_only_known_transient_errors(monkeypatch, error_type, retryable):
    # Keep the production lookup facade: its legacy error swallowing must not hide
    # a queued upload's outage and let it proceed into storage.
    monkeypatch.setattr(repository, "_find_duplicate_document", AsyncMock(side_effect=error_type("synthetic")))
    monkeypatch.setattr(processor.metrics_collector, "record_error", AsyncMock())
    writes = []

    async def save(*args, **kwargs):
        writes.append(args)

    monkeypatch.setattr(processor, "save_document_content", save)
    monkeypatch.setattr(
        processor.document_processor,
        "_process_pdf_unified",
        AsyncMock(return_value={"content": "synthetic", "pages": 1}),
    )
    result = await processor.process_uploaded_document(b"%PDF synthetic", "synthetic.pdf", 1)
    assert result["error"] == "storage_error"
    assert result["error_stage"] == "duplicate_lookup"
    assert result["write_started"] is False
    assert result["retryable"] is retryable
    assert writes == []
    outcome = await TaskQueue()._handle_document_processing(
        file_data=b"%PDF synthetic", filename="synthetic.pdf", user_id=1
    )
    assert outcome.outcome is (JobOutcome.RETRYABLE_FAILURE if retryable else JobOutcome.PERMANENT_FAILURE)


@pytest.mark.parametrize("error_type", [asyncpg.CannotConnectNowError, TimeoutError, ConnectionError])
async def test_transient_error_after_storage_start_is_never_retried(monkeypatch, error_type):
    monkeypatch.setattr(repository, "_find_duplicate_document", AsyncMock(return_value=None))
    monkeypatch.setattr(processor.metrics_collector, "record_error", AsyncMock())
    monkeypatch.setattr(processor, "save_document_content", AsyncMock(side_effect=error_type("synthetic")))
    monkeypatch.setattr(
        processor.document_processor,
        "_process_pdf_unified",
        AsyncMock(return_value={"content": "synthetic", "pages": 1}),
    )
    result = await processor.process_uploaded_document(b"%PDF synthetic", "synthetic.pdf", 1)
    assert result["error"] == "storage_error"
    assert result["error_stage"] == "storage_write"
    assert result["write_started"] is True
    assert result["retryable"] is False
    outcome = await TaskQueue()._handle_document_processing(
        file_data=b"%PDF synthetic", filename="synthetic.pdf", user_id=1
    )
    assert outcome.outcome is JobOutcome.UNKNOWN_DELIVERY
    assert outcome.reason_code == "document_storage_unconfirmed"


async def test_committed_document_with_disconnected_reply_has_unknown_outcome(monkeypatch):
    persisted = []

    async def commit_then_disconnect(*args, **kwargs):
        persisted.append(args[1])
        raise asyncpg.ConnectionDoesNotExistError("synthetic disconnect after commit")

    monkeypatch.setattr(repository, "_find_duplicate_document", AsyncMock(return_value=None))
    monkeypatch.setattr(processor.metrics_collector, "record_error", AsyncMock())
    monkeypatch.setattr(processor, "save_document_content", commit_then_disconnect)
    monkeypatch.setattr(
        processor.document_processor,
        "_process_pdf_unified",
        AsyncMock(return_value={"content": "synthetic", "pages": 1}),
    )
    result = await TaskQueue()._handle_document_processing(
        file_data=b"%PDF synthetic", filename="committed.pdf", user_id=1
    )
    assert persisted == ["committed.pdf"]
    assert result.outcome is JobOutcome.UNKNOWN_DELIVERY
    assert result.reason_code == "document_storage_unconfirmed"


async def test_unclassified_document_processor_exception_has_unknown_storage_phase(monkeypatch):
    monkeypatch.setattr(processor, "process_uploaded_document", AsyncMock(side_effect=RuntimeError("synthetic")))
    result = await TaskQueue()._handle_document_processing(
        file_data=b"%PDF synthetic", filename="synthetic.pdf", user_id=1
    )
    assert result.outcome is JobOutcome.UNKNOWN_DELIVERY
    assert result.reason_code == "document_storage_unconfirmed"


@pytest.mark.parametrize("user_id", ["invalid", [], float("inf")])
async def test_invalid_document_identity_remains_permanent(user_id):
    result = await TaskQueue()._handle_document_processing(
        file_data=b"%PDF synthetic", filename="synthetic.pdf", user_id=user_id
    )
    assert result.outcome is JobOutcome.PERMANENT_FAILURE


async def test_processing_style_exception_after_storage_started_is_unknown(monkeypatch):
    monkeypatch.setattr(repository, "_find_duplicate_document", AsyncMock(return_value=None))
    monkeypatch.setattr(processor.metrics_collector, "record_error", AsyncMock())
    monkeypatch.setattr(processor, "save_document_content", AsyncMock(side_effect=OSError("synthetic")))
    monkeypatch.setattr(
        processor.document_processor,
        "_process_pdf_unified",
        AsyncMock(return_value={"content": "synthetic", "pages": 1}),
    )
    result = await TaskQueue()._handle_document_processing(
        file_data=b"%PDF synthetic", filename="synthetic.pdf", user_id=1
    )
    assert result.outcome is JobOutcome.UNKNOWN_DELIVERY
    assert result.reason_code == "document_storage_unconfirmed"


async def test_default_duplicate_lookup_retains_legacy_error_contract(monkeypatch):
    monkeypatch.setattr(
        repository, "_find_duplicate_document", AsyncMock(side_effect=asyncpg.CannotConnectNowError("synthetic"))
    )
    assert await repository.check_duplicate_file(1, "synthetic-hash", "synthetic.pdf") is None


async def test_invalid_document_does_not_become_retryable(monkeypatch):
    monkeypatch.setattr(repository, "_find_duplicate_document", AsyncMock(return_value=None))
    outcome = await TaskQueue()._handle_document_processing(file_data=b"invalid", filename="synthetic.pdf", user_id=1)
    assert outcome.outcome is JobOutcome.PERMANENT_FAILURE
