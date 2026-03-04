"""
Row Level Security configuration and policy management.

Extracted from app/database.py to reduce monolith size.
"""

import logging
import re

import asyncpg

# --- RLS Policy Templates ---

RLS_POLICY_USER = """
CREATE POLICY {policy_name} ON {table_name}
FOR ALL USING (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
);
"""

RLS_POLICY_ADMIN = """
CREATE POLICY {policy_name} ON {table_name}
FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
"""

RLS_POLICY_GROUP = """
CREATE POLICY {policy_name} ON {table_name}
FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true' OR
    EXISTS (
        SELECT 1 FROM group_members gm
        WHERE gm.chat_id = {table_name}.chat_id
        AND gm.user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint)
    )
);
"""

RLS_POLICY_CONVERSATION_MESSAGES = """
CREATE POLICY {policy_name} ON {table_name}
FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true'
    OR owner_user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint)
);
"""

# --- RLS Configuration Map ---

RLS_CONFIG = {
    "users": [{"name": "users_policy", "template": RLS_POLICY_USER}],
    "chats": [{"name": "chats_policy", "template": RLS_POLICY_USER}],
    "active_chat_messages": [{"name": "active_chat_messages_policy", "template": RLS_POLICY_USER}],
    "user_documents": [{"name": "user_documents_policy", "template": RLS_POLICY_USER}],
    "user_roles": [{"name": "user_roles_policy", "template": RLS_POLICY_USER}],
    "user_state": [{"name": "user_state_policy", "template": RLS_POLICY_USER}],
    "user_metrics": [{"name": "user_metrics_policy", "template": RLS_POLICY_USER}],
    "feedback": [{"name": "feedback_policy", "template": RLS_POLICY_USER}],
    "conversations": [{"name": "conversations_policy", "template": RLS_POLICY_USER}],
    "roles": [
        {
            "name": "roles_read_policy",
            "sql": "CREATE POLICY roles_read_policy ON roles FOR SELECT USING (true);",
        },
        {
            "name": "roles_insert_policy",
            "sql": "CREATE POLICY roles_insert_policy ON roles FOR INSERT WITH CHECK ((select current_setting('app.is_admin', true)) = 'true');",
        },
        {
            "name": "roles_update_policy",
            "sql": "CREATE POLICY roles_update_policy ON roles FOR UPDATE USING ((select current_setting('app.is_admin', true)) = 'true');",
        },
        {
            "name": "roles_delete_policy",
            "sql": "CREATE POLICY roles_delete_policy ON roles FOR DELETE USING ((select current_setting('app.is_admin', true)) = 'true');",
        },
    ],
    "conversation_messages": [
        {
            "name": "conversation_messages_policy",
            "template": RLS_POLICY_CONVERSATION_MESSAGES,
        }
    ],
    "schema_migrations": [{"name": "schema_migrations_policy", "template": RLS_POLICY_ADMIN}],
    "group_chats": [{"name": "group_chats_policy", "template": RLS_POLICY_GROUP}],
    "group_members": [{"name": "group_members_policy", "template": RLS_POLICY_GROUP}],
    "group_messages": [{"name": "group_messages_policy", "template": RLS_POLICY_GROUP}],
    "api_keys": [{"name": "api_keys_policy", "template": RLS_POLICY_ADMIN}],
    "key_usage": [{"name": "key_usage_policy", "template": RLS_POLICY_ADMIN}],
    "tavily_api_keys": [
        {"name": "tavily_api_keys_policy", "template": RLS_POLICY_ADMIN}
    ],
    "tavily_key_usage": [
        {"name": "tavily_key_usage_policy", "template": RLS_POLICY_ADMIN}
    ],
    "openrouter_api_keys": [
        {"name": "openrouter_api_keys_policy", "template": RLS_POLICY_ADMIN}
    ],
    "openrouter_key_usage": [
        {"name": "openrouter_key_usage_policy", "template": RLS_POLICY_ADMIN}
    ],
    "metrics": [{"name": "metrics_policy", "template": RLS_POLICY_ADMIN}],
    "error_logs": [{"name": "error_logs_policy", "template": RLS_POLICY_ADMIN}],
    "model_configuration": [
        {"name": "model_configuration_policy", "template": RLS_POLICY_ADMIN}
    ],
    "long_term_memory": [
        {"name": "memory_user_isolation", "template": RLS_POLICY_USER}
    ],
    "key_model_status": [
        {"name": "key_model_status_admin_policy", "template": RLS_POLICY_ADMIN}
    ],
}

VALID_TABLES = set(RLS_CONFIG.keys())

# Regex for safely validating SQL identifiers (table names) before interpolation
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


async def setup_row_level_security(db_query):
    """Configure Row Level Security for all tables."""
    try:
        # Quick check if policies already exist (skip ALTER TABLE on every restart)
        existing = await db_query(
            "SELECT 1 FROM pg_policies WHERE tablename = 'users' AND policyname = 'users_policy'"
        )
        if existing:
            logging.info("RLS already configured, skipping setup.")
            return

        for table in VALID_TABLES:
            if not _SAFE_IDENTIFIER_RE.match(table):
                logging.error("Refusing to use unsafe table name in SQL: %s", table)
                continue
            try:
                await db_query(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
                await create_rls_policies(table, db_query)
            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logging.warning("Failed to enable RLS for table %s: %s", table, e)
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.error("Error setting up RLS: %s", e, exc_info=True)


async def create_rls_policies(table_name: str, db_query):
    """Create security policies for a table."""
    if table_name not in VALID_TABLES:
        logging.error("Invalid table name for RLS policy: %s", table_name)
        return

    try:
        policies = RLS_CONFIG.get(table_name)
        if not policies:
            logging.warning("No RLS configuration found for table: %s", table_name)
            return

        for policy_cfg in policies:
            policy_name = policy_cfg["name"]

            try:
                # Check if policy exists
                existing_policy = await db_query(
                    "SELECT 1 FROM pg_policies WHERE tablename = $1 AND policyname = $2",
                    (table_name, policy_name),
                )

                if not existing_policy:
                    # Construct SQL
                    if "sql" in policy_cfg:
                        sql = policy_cfg["sql"]
                    elif "template" in policy_cfg:
                        sql = policy_cfg["template"].format(
                            policy_name=policy_name, table_name=table_name
                        )
                    else:
                        logging.error(
                            "Missing SQL or template for policy %s", policy_name
                        )
                        continue

                    await db_query(sql)

            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logging.error(
                    "Failed to create policy %s for table %s: %s",
                    policy_name, table_name, e,
                )
                raise e

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.error("Error creating RLS policies for %s: %s", table_name, e, exc_info=True)
