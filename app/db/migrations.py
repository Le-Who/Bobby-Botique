"""
Database migrations — SQL file runner + legacy inline migrations.

Migration Workflow:
    1. ALL schema changes MUST have a numbered SQL file in scripts/migrations/.
    2. Changes applied via Supabase MCP/dashboard MUST be mirrored as a local
       SQL file so the app-level schema_migrations tracker stays in sync.
    3. The SQL files are the source of truth — they run on startup via this module.
    4. Legacy inline migrations (below) handle pre-existing environments only.

Extracted from app/database.py to reduce monolith size.
"""

import logging
import pathlib

import asyncpg


# ── Result container ──────────────────────────────────────────────────────────


class MigrationResult:
    """Returned by run_migrations() so callers can react to failures."""

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.failed: list[tuple[str, str]] = []  # (version, error_message)
        self.pending_at_start: int = 0

    @property
    def success(self) -> bool:
        return len(self.failed) == 0

    def __repr__(self) -> str:
        return (
            f"MigrationResult(applied={self.applied}, failed={self.failed}, "
            f"pending_at_start={self.pending_at_start})"
        )


# ── Public API ────────────────────────────────────────────────────────────────


async def run_migrations(db_query, db_manager) -> MigrationResult:
    """Run numbered SQL migration files from scripts/migrations/ with version tracking.

    Creates a `schema_migrations` table to track which migrations have been applied.
    Files are expected to be named NNN_description.sql (e.g. 001_create_metrics_tables.sql).
    Each file is executed inside a transaction; on error the migration is rolled back.

    Returns a MigrationResult. The caller is responsible for deciding whether to
    abort startup on failure — this function never raises.
    """
    result = MigrationResult()

    # 1. Create version tracking table if it doesn't exist
    try:
        await db_query("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
    except Exception as e:
        # If we can't even create the tracking table the DB has serious issues.
        # Surface as a failed migration so the caller can take action.
        logging.critical("Cannot create schema_migrations table: %s", e)
        result.failed.append(("schema_migrations", str(e)))
        return result

    # 2. Get already-applied versions
    applied = await db_query("SELECT version FROM schema_migrations ORDER BY version")
    applied_versions = {row["version"] for row in applied}

    # 3. Find migration files
    migrations_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "scripts" / "migrations"
    if not migrations_dir.exists():
        logging.info(
            "No migrations directory found at %s — skipping file migrations",
            migrations_dir,
        )
    else:
        sql_files = sorted(migrations_dir.glob("*.sql"))
        pending_files = [f for f in sql_files if f.stem.split("_", 1)[0] not in applied_versions]
        result.pending_at_start = len(pending_files)

        if pending_files:
            logging.info(
                "Schema drift: %d pending migration(s) found — applying now.",
                len(pending_files),
            )

        for sql_file in sql_files:
            version = sql_file.stem.split("_", 1)[0]

            if version in applied_versions:
                continue

            logging.info("Applying migration %s (%s)...", version, sql_file.name)

            try:
                async with db_manager.pool.acquire() as conn, conn.transaction():
                    sql_content = sql_file.read_text(encoding="utf-8")
                    await conn.execute(sql_content)

                    await conn.execute(
                        "INSERT INTO schema_migrations (version, filename) VALUES ($1, $2)"
                        " ON CONFLICT (version) DO NOTHING",
                        version,
                        sql_file.name,
                    )

                applied_versions.add(version)
                result.applied.append(version)
                logging.info("Migration %s applied successfully.", version)

            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                error_msg = str(e)
                result.failed.append((version, error_msg))
                logging.critical(
                    "Migration %s (%s) FAILED: %s",
                    version,
                    sql_file.name,
                    e,
                    exc_info=True,
                )
                # Hard-stop: don't apply dependent migrations on a broken predecessor.
                # This matches the behaviour of dedicated tools (Flyway, Alembic) and
                # prevents a half-applied schema from masking the root failure.
                logging.critical(
                    "Aborting further migrations after %s failure. "
                    "Fix the migration and redeploy.",
                    version,
                )
                break

    if result.applied:
        logging.info(
            "Migrations complete: %d applied, %d failed.",
            len(result.applied),
            len(result.failed),
        )
    elif result.failed:
        logging.critical(
            "Migrations FAILED: 0 applied, %d failed. Schema may be inconsistent!",
            len(result.failed),
        )

    # 4. Legacy inline migrations (for environments without SQL files)
    #    These are idempotent column-add checks that run on every startup.
    await _run_legacy_migrations(db_query)

    return result


# ── Legacy inline migrations ─────────────────────────────────────────────────


async def _run_legacy_migrations(db_query):
    """Legacy inline migrations — idempotent column-add checks."""
    try:
        doc_columns = await db_query(
            "SELECT column_name FROM information_schema.columns WHERE table_name='user_documents'"
        )
        doc_column_names = {c["column_name"] for c in doc_columns}

        if "filename" not in doc_column_names and "file_name" in doc_column_names:
            await db_query("ALTER TABLE user_documents RENAME COLUMN file_name TO filename;")
        elif "filename" not in doc_column_names:
            await db_query("ALTER TABLE user_documents ADD COLUMN filename TEXT;")

        required_columns = {
            "content": "TEXT",
            "pages": "INTEGER",
            "file_size": "BIGINT",
            "created_at": "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP",
        }
        cols_to_add = []
        for col, col_type in required_columns.items():
            if col not in doc_column_names:
                cols_to_add.append(f"ADD COLUMN {col} {col_type}")

        if cols_to_add:
            await db_query(f"ALTER TABLE user_documents {', '.join(cols_to_add)};")

        users_columns = await db_query(
            "SELECT column_name FROM information_schema.columns WHERE table_name='users'"
        )
        user_col_names = {c["column_name"] for c in users_columns}

        if "is_deep_dive" not in user_col_names:
            await db_query("ALTER TABLE users ADD COLUMN is_deep_dive BOOLEAN DEFAULT FALSE;")

        if "deep_dive_thread_id" not in user_col_names:
            await db_query("ALTER TABLE users ADD COLUMN deep_dive_thread_id TEXT;")

        # Ensure chats table has ltm_enabled column
        chats_columns = await db_query(
            "SELECT column_name FROM information_schema.columns WHERE table_name='chats'"
        )
        chats_col_names = {c["column_name"] for c in chats_columns}
        if "ltm_enabled" not in chats_col_names:
            await db_query("ALTER TABLE chats ADD COLUMN ltm_enabled BOOLEAN DEFAULT TRUE;")

        if "tts_temperature" not in chats_col_names:
            await db_query("ALTER TABLE chats ADD COLUMN tts_temperature FLOAT;")

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Legacy migration warning: %s", e)
