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


async def create_tables(db_query):
    """Validate that all expected application tables exist.

    Table creation is handled by SQL migration files.  This function only
    checks that every table the code depends on is present, and logs
    warnings for any that are missing so operators can investigate.
    """
    try:
        rows = await db_query("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        existing = {row["tablename"] for row in rows}
        missing = EXPECTED_TABLES - existing

        if missing:
            logging.warning(
                "Schema validation: %d expected table(s) missing from public schema: %s. "
                "Migrations may not have run yet — they will execute next.",
                len(missing),
                ", ".join(sorted(missing)),
            )
        else:
            logging.info(
                "Schema validation passed: all %d expected tables present.",
                len(EXPECTED_TABLES),
            )
    except Exception as e:
        # Non-fatal — migrations will create missing tables momentarily.
        logging.warning("Schema validation skipped due to error: %s", e)
