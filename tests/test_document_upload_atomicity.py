"""Upload outcomes through the real processor/repository and a bounded driver fake.

The driver models connection ownership, staged writes and commit failures. It is
not evidence for PostgreSQL isolation, query planning or network acknowledgments.
"""

import asyncio
import hashlib
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app import database
from app import document_processor as processor_module
from app.documents import repository


class UploadPool:
    _closed = False

    def __init__(self):
        self.rows = {
            owner: [
                {
                    "id": doc_id,
                    "user_id": owner,
                    "filename": f"saved-{doc_id}.pdf",
                    "created_at": datetime(2026, 1, 1),
                    "file_hash": f"hash-{doc_id}",
                    "content": "saved content",
                }
                for doc_id in ids
            ]
            for owner, ids in ((101, (1, 2, 3, 4, 5)), (202, (22,)))
        }
        self.next_id = 100
        self.failure = None
        self.connections = []
        self.locks = {}
        self.pause_insert = False
        self.insert_entered = asyncio.Event()
        self.release_insert = asyncio.Event()
        self.lock_waiting = asyncio.Event()
        self.events = []
        self.clock = 0
        self.pause_first_lock = False
        self.first_lock_entered = asyncio.Event()
        self.release_first_lock = asyncio.Event()

    def now(self):
        self.clock += 1
        return datetime(2026, 1, 1) + timedelta(microseconds=self.clock)

    @asynccontextmanager
    async def acquire(self):
        connection = UploadConnection(self)
        self.connections.append(connection)
        yield connection


class UploadConnection:
    def __init__(self, pool):
        self.pool = pool
        self.in_transaction = False
        self.staged = {}
        self.held_lock = None
        self.context_user = None
        self.transaction_time = None

    @asynccontextmanager
    async def transaction(self):
        self.in_transaction = True
        self.transaction_time = self.pool.now()
        try:
            yield self
            if self.pool.failure == "commit":
                raise asyncpg.PostgresError("synthetic commit failure")
            self.pool.rows.update(self.staged)
            self.pool.events.append(("commit", self))
        finally:
            self.staged.clear()
            self.in_transaction = False
            if self.held_lock is not None:
                self.held_lock.release()
                self.held_lock = None

    async def execute(self, query, *params):
        assert "pg_advisory_xact_lock" in query
        assert self.in_transaction
        assert params == (self.context_user,)
        if self.pool.pause_first_lock:
            self.pool.pause_first_lock = False
            self.pool.first_lock_entered.set()
            await self.pool.release_first_lock.wait()
        lock = self.pool.locks.setdefault(params[0], asyncio.Lock())
        if lock.locked():
            self.pool.lock_waiting.set()
        await lock.acquire()
        self.held_lock = lock
        self.pool.events.append(("lock", self))

    async def fetch(self, query, *params):
        sql = " ".join(query.split())
        if "set_config" in sql:
            self.context_user = int(params[0]) if params else None
            return []
        owner = params[0]
        rows = self.staged.get(owner, self.pool.rows.get(owner, []))
        if sql.startswith("SELECT COUNT"):
            return [{"doc_count": len(rows)}]
        if "AND file_hash = $2" in sql:
            return [row.copy() for row in rows if row["file_hash"] == params[1]]
        if "split_part(file_hash, ':', 1) = $2" in sql:
            matches = [row for row in rows if row["file_hash"].split(":", 1)[0] == params[1]]
            matches.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
            return [{key: row[key] for key in ("id", "filename", "created_at")} for row in matches[:1]]
        if sql.startswith("INSERT INTO user_documents"):
            if self.pool.failure == "insert":
                # The old standalone-query retry layer exits immediately for this
                # synthetic error; the repository must still propagate failure.
                raise asyncpg.PostgresError("rate limit: synthetic insert failure")
            if self.pool.pause_insert:
                self.pool.insert_entered.set()
                await self.pool.release_insert.wait()
            if any(row["file_hash"] == params[5] for row in rows):
                # The real schema has UNIQUE(user_id, file_hash). Keep this
                # constraint so forced copies cannot pass with invalid SQL.
                raise asyncpg.UniqueViolationError("synthetic user_documents hash conflict")
            row = {
                "id": self.pool.next_id,
                "user_id": owner,
                "filename": params[1],
                "content": params[2],
                "pages": params[3],
                "file_size": params[4],
                "file_hash": params[5],
                "created_at": self.pool.now()
                if "clock_timestamp()" in sql
                else self.transaction_time or self.pool.now(),
            }
            self.pool.next_id += 1
            self._write(owner, [*rows, row])
            self.pool.events.append(("insert", self))
            return []
        if sql.startswith("DELETE FROM user_documents") and "OFFSET $2" in sql:
            if self.pool.failure == "retention":
                raise asyncpg.PostgresError("synthetic retention failure")
            ordering = (
                (lambda row: (row["created_at"], row["id"])) if "id DESC" in sql else lambda row: row["created_at"]
            )
            ordered = sorted(rows, key=ordering, reverse=True)
            self._write(owner, ordered[: params[1]])
            self.pool.events.append(("retention", self))
            return [{"id": row["id"]} for row in ordered[params[1] :]]
        raise AssertionError(f"Unexpected upload query: {sql}")

    def _write(self, owner, rows):
        target = self.staged if self.in_transaction else self.pool.rows
        target[owner] = rows


@pytest.fixture
def uploads(monkeypatch):
    pool = UploadPool()
    monkeypatch.setattr(database.db_manager, "pool", pool)
    monkeypatch.setattr(database.db_manager, "reconnect", AsyncMock())
    monkeypatch.setattr(repository.settings, "MAX_DOCUMENTS_PER_USER", 5)
    monkeypatch.setattr(processor_module.metrics_collector, "record_error", AsyncMock())
    return SimpleNamespace(pool=pool, processor=processor_module.DocumentProcessor())


def input_data(tmp_path, suffix, is_path):
    data = b"%PDF synthetic content" if suffix == ".pdf" else b"PK\x03\x04synthetic content"
    if not is_path:
        return data
    path = tmp_path / f"input{suffix}"
    path.write_bytes(data)
    return str(path)


def parsed_document(monkeypatch, processor, suffix):
    result = {"success": True, "content": "parsed document", "pages": 1, "text_length": 15}
    method = "_process_pdf_sync" if suffix == ".pdf" else "_process_word_sync"
    monkeypatch.setattr(processor, method, lambda *_args: result.copy())


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_id", [1, 5])
async def test_duplicate_at_capacity_preserves_every_saved_id(uploads, duplicate_id):
    data = b"%PDF duplicate"
    uploads.pool.rows[101][duplicate_id - 1]["file_hash"] = hashlib.sha256(data).hexdigest()
    before = deepcopy(uploads.pool.rows)

    result = await uploads.processor.process_document(data, "copy.pdf", 101)

    assert result["error"] == "duplicate"
    assert uploads.pool.rows == before
    assert uploads.pool.events == []


@pytest.mark.asyncio
async def test_confirmed_duplicate_uses_common_save_and_retention(uploads, monkeypatch):
    data = b"%PDF duplicate"
    uploads.pool.rows[101][0]["file_hash"] = hashlib.sha256(data).hexdigest()
    parsed_document(monkeypatch, uploads.processor, ".pdf")
    monkeypatch.setattr(repository.settings, "MAX_DOCUMENTS_PER_USER", 3)

    result = await uploads.processor.process_document(data, "duplicate.pdf", 101, skip_duplicate_check=True)

    assert result["success"] is True
    assert [row["id"] for row in uploads.pool.rows[101]] == [100, 5, 4]
    stored_hash = uploads.pool.rows[101][0]["file_hash"]
    assert stored_hash.startswith(hashlib.sha256(data).hexdigest() + ":copy:")
    assert len(stored_hash.rsplit(":", 1)[1]) == 32
    assert [event for event, _ in uploads.pool.events] == ["lock", "insert", "retention", "commit"]


@pytest.mark.asyncio
async def test_ordinary_upload_detects_forced_copy_after_original_is_removed(uploads, monkeypatch):
    data = b"%PDF original evicted"
    canonical_hash = hashlib.sha256(data).hexdigest()
    uploads.pool.rows[101][0]["file_hash"] = canonical_hash
    parsed_document(monkeypatch, uploads.processor, ".pdf")
    monkeypatch.setattr(repository.settings, "MAX_DOCUMENTS_PER_USER", 1)

    forced = await uploads.processor.process_document(data, "forced.pdf", 101, skip_duplicate_check=True)
    assert forced["success"] is True
    assert [row["id"] for row in uploads.pool.rows[101]] == [100]
    duplicate = await uploads.processor.process_document(data, "ordinary.pdf", 101)

    assert duplicate["error"] == "duplicate"
    assert duplicate["duplicate_info"]["id"] == 100
    assert [row["id"] for row in uploads.pool.rows[101]] == [100]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".pdf", ".docx"])
@pytest.mark.parametrize("is_path", [False, True])
async def test_invalid_document_at_capacity_preserves_saved_documents(uploads, tmp_path, suffix, is_path):
    data = b"invalid content"
    if is_path:
        path = tmp_path / f"invalid{suffix}"
        path.write_bytes(data)
        data = str(path)
    before = deepcopy(uploads.pool.rows)

    result = await uploads.processor.process_document(data, f"invalid{suffix}", 101, is_path=is_path)

    assert result.get("error")
    assert uploads.pool.rows == before
    assert uploads.pool.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".pdf", ".docx"])
@pytest.mark.parametrize("is_path", [False, True])
@pytest.mark.parametrize("failure", ["insert", "retention", "commit"])
async def test_storage_failure_never_returns_success_or_retains_partial_changes(
    uploads, monkeypatch, tmp_path, suffix, is_path, failure
):
    uploads.pool.failure = failure
    before = deepcopy(uploads.pool.rows)
    parsed_document(monkeypatch, uploads.processor, suffix)

    result = await uploads.processor.process_document(
        input_data(tmp_path, suffix, is_path), f"new{suffix}", 101, is_path=is_path
    )

    assert result.get("error")
    assert not result.get("success")
    assert uploads.pool.rows == before
    assert all(not conn.in_transaction and conn.held_lock is None for conn in uploads.pool.connections)


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity, expected_ids", [(1, [100]), (3, [100, 5, 4]), (7, [100, 5, 4, 3, 2, 1])])
async def test_success_commits_insert_and_deterministic_configured_retention(
    uploads, monkeypatch, capacity, expected_ids
):
    monkeypatch.setattr(repository.settings, "MAX_DOCUMENTS_PER_USER", capacity)
    parsed_document(monkeypatch, uploads.processor, ".pdf")

    result = await uploads.processor.process_document(b"%PDF new", "new.pdf", 101)

    assert result["success"] is True
    assert [row["id"] for row in uploads.pool.rows[101]] == expected_ids
    assert [row["id"] for row in uploads.pool.rows[202]] == [22]
    assert [event for event, _ in uploads.pool.events] == ["lock", "insert", "retention", "commit"]
    assert len({id(conn) for _, conn in uploads.pool.events}) == 1


@pytest.mark.asyncio
async def test_overlapping_saves_wait_for_same_user_transaction_before_inserting(uploads, monkeypatch):
    monkeypatch.setattr(repository.settings, "MAX_DOCUMENTS_PER_USER", 2)
    uploads.pool.pause_insert = True
    first = asyncio.create_task(repository.save_document_content(101, "first.pdf", "first", 1, "first"))
    second = None
    try:
        await asyncio.wait_for(uploads.pool.insert_entered.wait(), 1)
        second = asyncio.create_task(repository.save_document_content(101, "second.pdf", "second", 1, "second"))
        await asyncio.wait_for(uploads.pool.lock_waiting.wait(), 1)
        assert uploads.pool.events == [("lock", uploads.pool.events[0][1])]
        uploads.pool.release_insert.set()
        await asyncio.gather(first, second)
    finally:
        uploads.pool.release_insert.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task

    assert [row["filename"] for row in uploads.pool.rows[101]] == ["second.pdf", "first.pdf"]
    assert [event for event, _ in uploads.pool.events] == [
        "lock",
        "insert",
        "retention",
        "commit",
        "lock",
        "insert",
        "retention",
        "commit",
    ]


@pytest.mark.asyncio
async def test_overlapping_same_hash_upload_returns_existing_duplicate_without_second_eviction(uploads, monkeypatch):
    parsed_document(monkeypatch, uploads.processor, ".pdf")
    uploads.pool.pause_insert = True
    data = b"%PDF same overlapping upload"
    first = asyncio.create_task(uploads.processor.process_document(data, "first.pdf", 101))
    second = None
    try:
        await asyncio.wait_for(uploads.pool.insert_entered.wait(), 2)
        second = asyncio.create_task(uploads.processor.process_document(data, "second.pdf", 101))
        await asyncio.wait_for(uploads.pool.lock_waiting.wait(), 2)
        uploads.pool.release_insert.set()
        first_result, second_result = await asyncio.gather(first, second)
    finally:
        uploads.pool.release_insert.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task

    assert first_result["success"] is True
    assert second_result["error"] == "duplicate"
    assert second_result["duplicate_info"]["id"] == 100
    assert second_result["duplicate_info"]["filename"] == "first.pdf"
    assert [row["id"] for row in uploads.pool.rows[101]] == [100, 5, 4, 3, 2]
    assert [event for event, _ in uploads.pool.events].count("insert") == 1
    assert [event for event, _ in uploads.pool.events].count("retention") == 1
    assert [row["id"] for row in uploads.pool.rows[202]] == [22]


@pytest.mark.asyncio
async def test_save_uses_lock_acquisition_order_instead_of_transaction_start_for_retention(uploads, monkeypatch):
    monkeypatch.setattr(repository.settings, "MAX_DOCUMENTS_PER_USER", 1)
    uploads.pool.pause_first_lock = True
    earlier = asyncio.create_task(repository.save_document_content(101, "earlier-tx.pdf", "earlier", 1, "earlier"))
    try:
        await asyncio.wait_for(uploads.pool.first_lock_entered.wait(), 2)
        await repository.save_document_content(101, "later-tx.pdf", "later", 1, "later")
        assert [row["filename"] for row in uploads.pool.rows[101]] == ["later-tx.pdf"]
        uploads.pool.release_first_lock.set()
        await earlier
    finally:
        uploads.pool.release_first_lock.set()
        if not earlier.done():
            earlier.cancel()
        with suppress(asyncio.CancelledError):
            await earlier

    assert [row["filename"] for row in uploads.pool.rows[101]] == ["earlier-tx.pdf"]
