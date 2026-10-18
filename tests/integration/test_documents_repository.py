"""Document retention and copy identity against the migrated PostgreSQL schema.

The existing disposable connection fixture owns the outer rollback. Repository
transactions use savepoints here; concurrent ownership has separate offline
barrier tests, and these cases do not claim commit-acknowledgment coverage.
"""

import asyncpg
import pytest

from app.documents import repository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def saved_documents(conn, user_id):
    rows = await conn.fetch(
        "SELECT id, filename, content, file_hash FROM user_documents WHERE user_id = $1 "
        "ORDER BY created_at DESC, id DESC",
        user_id,
    )
    return [dict(row) for row in rows]


async def test_retention_ties_duplicates_and_forced_copy_identity(db_conn_with_user, test_user_id, monkeypatch):
    conn = db_conn_with_user
    monkeypatch.setattr(repository.settings, "MAX_DOCUMENTS_PER_USER", 3)
    for name in ("alpha", "beta", "gamma"):
        assert await repository.save_document_content(test_user_id, name + ".pdf", name, 1, name) is None
    await conn.execute(
        "UPDATE user_documents SET created_at = TIMESTAMPTZ '2000-01-01 00:00:00+00' WHERE user_id = $1", test_user_id
    )
    original_gamma = (await saved_documents(conn, test_user_id))[0]

    await repository.save_document_content(test_user_id, "delta.pdf", "delta", 1, "delta")
    before = await saved_documents(conn, test_user_id)
    assert [row["filename"] for row in before] == ["delta.pdf", "gamma.pdf", "beta.pdf"]
    duplicate = await repository.save_document_content(test_user_id, "ordinary.pdf", "gamma", 1, "gamma")
    assert duplicate["id"] == original_gamma["id"]
    assert await saved_documents(conn, test_user_id) == before

    assert (
        await repository.save_document_content(
            test_user_id, "forced.pdf", "gamma", 1, "gamma", skip_duplicate_check=True
        )
        is None
    )
    copies = await saved_documents(conn, test_user_id)
    assert [row["filename"] for row in copies] == ["forced.pdf", "delta.pdf", "gamma.pdf"]
    assert copies[-1]["id"] == original_gamma["id"]
    assert copies[0]["file_hash"].startswith("gamma:copy:")
    assert copies[-1]["file_hash"] == "gamma"

    await conn.execute("DELETE FROM user_documents WHERE id = $1 AND user_id = $2", original_gamma["id"], test_user_id)
    duplicate = await repository.check_duplicate_file(test_user_id, "gamma", "ordinary.pdf")
    assert duplicate["id"] == copies[0]["id"]
    atomic_duplicate = await repository.save_document_content(test_user_id, "ordinary.pdf", "gamma", 1, "gamma")
    assert atomic_duplicate["id"] == copies[0]["id"]


@pytest.mark.parametrize("failure_after", ["INSERT INTO user_documents", "DELETE FROM user_documents"])
async def test_mutation_failure_rolls_back_insert_and_retention(
    db_conn_with_user, test_user_id, monkeypatch, failure_after
):
    conn = db_conn_with_user
    monkeypatch.setattr(repository.settings, "MAX_DOCUMENTS_PER_USER", 1)
    await repository.save_document_content(test_user_id, "saved.pdf", "saved", 1, "saved")
    before = await saved_documents(conn, test_user_id)
    real_query = repository.database.db_query

    async def fail_after_mutation(query, *args, **kwargs):
        result = await real_query(query, *args, **kwargs)
        if query.lstrip().startswith(failure_after):
            raise asyncpg.PostgresError("synthetic failure after real document mutation")
        return result

    monkeypatch.setattr(repository.database, "db_query", fail_after_mutation)
    with pytest.raises(asyncpg.PostgresError, match="synthetic failure"):
        await repository.save_document_content(test_user_id, "rejected.pdf", "rejected", 1, "rejected")
    assert await saved_documents(conn, test_user_id) == before


async def test_new_save_survives_retention_inside_an_earlier_opened_transaction(
    db_conn_with_user, test_user_id, monkeypatch
):
    conn = db_conn_with_user
    monkeypatch.setattr(repository.settings, "MAX_DOCUMENTS_PER_USER", 1)
    # The fixture's outer transaction predates this row. CURRENT_TIMESTAMP for
    # the later save would still be older than this row's wall-clock timestamp.
    await conn.execute(
        "INSERT INTO user_documents(user_id, filename, content, file_hash, created_at) "
        "VALUES($1, 'existing.pdf', 'existing', 'existing', clock_timestamp())",
        test_user_id,
    )
    await repository.save_document_content(test_user_id, "new.pdf", "new", 1, "new")

    assert [row["filename"] for row in await saved_documents(conn, test_user_id)] == ["new.pdf"]
