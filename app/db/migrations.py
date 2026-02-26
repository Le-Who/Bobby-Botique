"""
Database migrations — SQL file runner + legacy inline migrations.

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
        logging.info("No migrations directory found at %s — skipping file migrations", migrations_dir)
    else:
        sql_files = sorted(migrations_dir.glob("*.sql"))

        for sql_file in sql_files:
            # Extract version: "001" from "001_create_metrics_tables.sql"
            version = sql_file.stem.split("_", 1)[0]

            if version in applied_versions:
                continue  # Already applied

            logging.info("Applying migration %s (%s)...", version, sql_file.name)

            try:
                sql_content = sql_file.read_text(encoding="utf-8")

                # Execute inside a transaction
                async with db_manager.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(sql_content)
                        await conn.execute(
                            "INSERT INTO schema_migrations (version, filename) VALUES ($1, $2)",
                            version, sql_file.name,
                        )

                logging.info("Migration %s applied successfully.", version)
                applied_versions.add(version)

            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logging.error("Migration %s FAILED: %s", version, e)
                # Don't halt startup — log and continue
                break  # Stop at first failure to preserve order

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
            await db_query(
                "ALTER TABLE user_documents RENAME COLUMN file_name TO filename;"
            )
        elif "filename" not in doc_column_names:
            await db_query("ALTER TABLE user_documents ADD COLUMN filename TEXT;")

        required_columns = {
            "content": "TEXT",
            "pages": "INTEGER",
            "file_size": "BIGINT",
            "created_at": "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP",
        }
        for col, col_type in required_columns.items():
            if col not in doc_column_names:
                await db_query(
                    f"ALTER TABLE user_documents ADD COLUMN {col} {col_type};"
                )

        users_columns = await db_query(
            "SELECT column_name FROM information_schema.columns WHERE table_name='users'"
        )
        user_col_names = {c["column_name"] for c in users_columns}

        if "is_deep_dive" not in user_col_names:
            await db_query(
                "ALTER TABLE users ADD COLUMN is_deep_dive BOOLEAN DEFAULT FALSE;"
            )

        if "deep_dive_thread_id" not in user_col_names:
            await db_query("ALTER TABLE users ADD COLUMN deep_dive_thread_id TEXT;")

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Legacy migration warning: %s", e)
