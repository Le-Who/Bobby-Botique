"""
Database schema validation — verifies all expected tables exist after migrations.

Previously this module held CREATE TABLE IF NOT EXISTS statements, but all DDL
is now managed exclusively via SQL migration files in scripts/migrations/.
This module runs on startup to catch drift between expected and actual schema.
"""

import logging

# Every table the application depends on.  Kept in sync with 000_init_schema.sql
# and subsequent CREATE TABLE migrations.
EXPECTED_TABLES = frozenset(
    {
        # Core (000_init_schema)
        "users",
        "chats",
        "roles",
        "user_roles",
        "conversations",
        "conversation_messages",
        "api_keys",
        "key_usage",
        "metrics",
        "crocodile_daily_days",
        "crocodile_daily_puzzles",
        "crocodile_daily_preferences",
        "crocodile_daily_results",
        "crocodile_daily_result_messages",
        "crocodile_daily_prompt_messages",
        "crocodile_player_activity",
        "daily_2048_puzzles",
        "daily_2048_prompt_messages",
        "daily_2048_results",
        "daily_trivia_puzzles",
        "daily_trivia_facts",
        "daily_trivia_question_variants",
        "daily_trivia_puzzle_revisions",
        "daily_trivia_question_occurrences",
        "daily_trivia_results",
        "daily_trivia_super_results",
        "daily_trivia_prompt_messages",
        "daily_trivia_preferences",
        "daily_trivia_used_keys",
        "error_logs",
        "tarot_daily_readings",
        "tavily_api_keys",
        "tavily_key_usage",
        "openrouter_api_keys",
        "openrouter_key_usage",
        "key_model_status",
        "user_documents",
        "user_state",
        "feedback",
        "horoscope_subscriptions",
        "inline_boards",
        "natal_reports",
        "tarot_daily_subscriptions",
        "user_achievements",
        # Added by later migrations / 018 backfill
        "user_metrics",
        "model_configuration",
        "active_chat_messages",
        "long_term_memory",
        "group_chats",
        "group_members",
        "group_messages",
        # GraphRAG memory (000/018 backfill + 025/026/026b/027)
        "memory_nodes",
        "memory_edges",
        "memory_edge_sources",
        "memory_node_sources",
        "memory_derivation_sources",
        "private_data_leases",
        # Feature tables (021, 022)
        "brief_subscriptions",
        "conversation_branches",
        "user_reminders",
        # Infrastructure
        "schema_migrations",
        # Runtime-configurable admin settings (034_global_settings)
        "global_settings",
    }
)

# Critical column-level contract for tables whose runtime shape cannot be
# inferred safely from table existence alone.
EXPECTED_COLUMNS = {
    "chats": frozenset(
        {
            "user_id",
            "model",
            "token_count",
            "search_enabled",
            "system_prompt",
            "context_summary",
            "thinking_level",
            "ltm_enabled",
            "memory_epoch",
            "private_data_blocked",
            "branch_id",
            "temperature",
            "voice_id",
            "tts_temperature",
            "live_voice_name",
            "live_thinking_level",
            "live_connection_mode",
        }
    )
}


class SchemaValidationError(RuntimeError):
    """Raised when the migrated public schema is incomplete or unreadable."""


async def validate_schema(db_query):
    """Validate that all expected application tables exist.

    Table creation is handled by SQL migration files.  This function only
    checks that every table the code depends on is present, and logs
    raises if any are missing so the process cannot start on partial DDL.
    """
    try:
        rows = await db_query("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        existing = {row["tablename"] for row in rows}
        missing = EXPECTED_TABLES - existing

        if missing:
            missing_tables = ", ".join(sorted(missing))
            raise SchemaValidationError(
                f"Schema validation failed: {len(missing)} expected table(s) missing "
                f"from public schema: {missing_tables}"
            )

        column_rows = await db_query(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY($1::text[])
            """,
            (list(EXPECTED_COLUMNS),),
        )
        existing_columns: dict[str, set[str]] = {table: set() for table in EXPECTED_COLUMNS}
        for row in column_rows:
            existing_columns[row["table_name"]].add(row["column_name"])

        missing_columns = {
            table: expected - existing_columns[table]
            for table, expected in EXPECTED_COLUMNS.items()
            if expected - existing_columns[table]
        }
        if missing_columns:
            details = "; ".join(
                f"{table}: {', '.join(sorted(columns))}" for table, columns in sorted(missing_columns.items())
            )
            raise SchemaValidationError(f"Schema validation failed: required column(s) missing: {details}")

        logging.info(
            "Schema validation passed: all %d expected tables and critical columns present.",
            len(EXPECTED_TABLES),
        )
    except SchemaValidationError:
        raise
    except Exception as e:
        raise SchemaValidationError(f"Schema validation query failed: {e}") from e


# Backward-compatible alias for integrations that imported the old name.
create_tables = validate_schema
