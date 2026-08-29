"""Repository-wide invariants for portable, fail-fast SQL migrations."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path("scripts/migrations")


def _migration_sql() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.sql")))


def _without_line_comments(sql: str) -> str:
    return re.sub(r"--.*$", "", sql, flags=re.MULTILINE)


def test_manifest_accepts_the_repository_migrations_in_deterministic_order() -> None:
    from app.db.migration_manifest import discover_migration_files

    files = discover_migration_files(MIGRATIONS_DIR)

    assert files
    assert files == sorted(files, key=lambda path: path.name)


@pytest.mark.parametrize(
    ("filenames", "expected_message"),
    [
        (("001_first.sql", "001_second.sql"), "duplicate migration version"),
        (("not_numbered.sql",), "invalid migration filename"),
    ],
)
def test_manifest_rejects_ambiguous_filenames(
    tmp_path: Path, filenames: tuple[str, ...], expected_message: str
) -> None:
    from app.db.migration_manifest import MigrationManifestError, discover_migration_files

    for filename in filenames:
        (tmp_path / filename).write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(MigrationManifestError, match=expected_message):
        discover_migration_files(tmp_path)


def test_manifest_rejects_empty_migration(tmp_path: Path) -> None:
    from app.db.migration_manifest import MigrationManifestError, discover_migration_files

    (tmp_path / "001_empty.sql").write_text(" \n", encoding="utf-8")

    with pytest.raises(MigrationManifestError, match="empty migration"):
        discover_migration_files(tmp_path)


def test_manifest_rejects_non_utf8_migration(tmp_path: Path) -> None:
    from app.db.migration_manifest import MigrationManifestError, discover_migration_files

    (tmp_path / "001_invalid.sql").write_bytes(b"SELECT '\xff';")

    with pytest.raises(MigrationManifestError, match="valid UTF-8"):
        discover_migration_files(tmp_path)


def test_fresh_database_migrations_use_the_runtime_rls_context_names() -> None:
    sql = _without_line_comments(_migration_sql())

    assert "app.current_user_id" not in sql
    assert "app.user_is_admin" not in sql


def test_service_role_policy_is_conditional_for_plain_postgres() -> None:
    sql = (MIGRATIONS_DIR / "014_add_key_model_status.sql").read_text(encoding="utf-8")

    role_check = sql.index("FROM pg_roles")
    policy_creation = sql.index("TO service_role")
    assert "rolname = 'service_role'" in sql
    assert role_check < policy_creation


@pytest.mark.parametrize(
    "filename",
    ["047_add_horoscope_subscriptions.sql", "054_add_tarot_daily_subscriptions.sql"],
)
def test_subscription_migrations_do_not_install_open_tenant_policies(filename: str) -> None:
    sql = _without_line_comments((MIGRATIONS_DIR / filename).read_text(encoding="utf-8"))

    assert not re.search(r"USING\s*\(\s*true\s*\)", sql, flags=re.IGNORECASE)


def test_schema_catalog_covers_every_durable_table_created_by_migrations() -> None:
    from app.db.schema import EXPECTED_TABLES

    sql = _without_line_comments(_migration_sql())
    created_tables = {
        match.group(1)
        for match in re.finditer(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(?:public\.)?([a-z_][a-z0-9_]*)",
            sql,
            flags=re.IGNORECASE,
        )
    }

    assert created_tables <= EXPECTED_TABLES


def test_runtime_rls_config_covers_every_table_enabled_by_migrations() -> None:
    from app.db.rls import RLS_CONFIG

    sql = _without_line_comments(_migration_sql())
    rls_tables = {
        match.group(1)
        for match in re.finditer(
            r"ALTER\s+TABLE\s+(?:public\.)?([a-z_][a-z0-9_]*)\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
            sql,
            flags=re.IGNORECASE,
        )
    }

    assert rls_tables <= RLS_CONFIG.keys()


def test_user_rls_template_explicitly_checks_inserted_and_updated_rows() -> None:
    from app.db.rls import RLS_POLICY_USER

    assert "USING" in RLS_POLICY_USER
    assert "WITH CHECK" in RLS_POLICY_USER


@pytest.mark.asyncio
async def test_schema_validation_fails_closed_when_a_table_is_missing() -> None:
    from app.db.schema import SchemaValidationError, validate_schema

    async def no_tables(_query: str):
        return []

    with pytest.raises(SchemaValidationError, match="missing"):
        await validate_schema(no_tables)
