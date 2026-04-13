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


async def run_migrations(db_query, db_manager):
    """Run numbered SQL migration files from scripts/migrations/ with version tracking.

    Creates a `schema_migrations` table to track which migrations have been applied.
    Files are expected to be named NNN_description.sql (e.g. 001_create_metrics_tables.sql).
    Each file is executed inside a transaction; on error the migration is rolled back.
    """
    # 1. Create version tracking table if it doesn't exist
    await db_query("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)

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
                        "INSERT INTO schema_migrations (version, filename) VALUES ($1, $2)",
                        version,
                        sql_file.name,
                    )

                applied_versions.add(version)
                logging.info("Migration %s applied successfully.", version)

            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logging.error(
                    "Migration %s (%s) FAILED — skipping: %s",
                    version,
                    sql_file.name,
                    e,
                    exc_info=True,
                )
                # Per-file isolation: continue to next migration

    # 4. Legacy inline migrations (for environments without SQL files)
    #    These are idempotent column-add checks that run on every startup.
    await _run_legacy_migrations(db_query)


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

        users_columns = await db_query("SELECT column_name FROM information_schema.columns WHERE table_name='users'")
        user_col_names = {c["column_name"] for c in users_columns}

        if "is_deep_dive" not in user_col_names:
            await db_query("ALTER TABLE users ADD COLUMN is_deep_dive BOOLEAN DEFAULT FALSE;")

        if "deep_dive_thread_id" not in user_col_names:
            await db_query("ALTER TABLE users ADD COLUMN deep_dive_thread_id TEXT;")

        # Ensure chats table has ltm_enabled column
        chats_columns = await db_query("SELECT column_name FROM information_schema.columns WHERE table_name='chats'")
        chats_col_names = {c["column_name"] for c in chats_columns}
        if "ltm_enabled" not in chats_col_names:
            await db_query("ALTER TABLE chats ADD COLUMN ltm_enabled BOOLEAN DEFAULT TRUE;")

        if "tts_temperature" not in chats_col_names:
            await db_query("ALTER TABLE chats ADD COLUMN tts_temperature FLOAT;")

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Legacy migration warning: %s", e)
