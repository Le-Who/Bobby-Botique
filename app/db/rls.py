"""
Row Level Security configuration and policy management.

Extracted from app/database.py to reduce monolith size.
"""

import logging
import re

# --- RLS Policy Templates ---

RLS_POLICY_USER = """
CREATE POLICY {policy_name} ON {table_name}
FOR ALL USING (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
)
WITH CHECK (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
);
"""

RLS_POLICY_ADMIN = """
CREATE POLICY {policy_name} ON {table_name}
FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true')
WITH CHECK ((select current_setting('app.is_admin', true)) = 'true');
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
)
WITH CHECK (
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
)
WITH CHECK (
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
    "tavily_api_keys": [{"name": "tavily_api_keys_policy", "template": RLS_POLICY_ADMIN}],
    "tavily_key_usage": [{"name": "tavily_key_usage_policy", "template": RLS_POLICY_ADMIN}],
    "openrouter_api_keys": [{"name": "openrouter_api_keys_policy", "template": RLS_POLICY_ADMIN}],
    "openrouter_key_usage": [{"name": "openrouter_key_usage_policy", "template": RLS_POLICY_ADMIN}],
    "metrics": [{"name": "metrics_policy", "template": RLS_POLICY_ADMIN}],
    "error_logs": [{"name": "error_logs_policy", "template": RLS_POLICY_ADMIN}],
    "model_configuration": [{"name": "model_configuration_policy", "template": RLS_POLICY_ADMIN}],
    "long_term_memory": [{"name": "memory_user_isolation", "template": RLS_POLICY_USER}],
    "memory_nodes": [{"name": "memory_nodes_user_policy", "template": RLS_POLICY_USER}],
    "memory_edges": [{"name": "memory_edges_user_policy", "template": RLS_POLICY_USER}],
    "memory_edge_sources": [{"name": "memory_edge_sources_user_policy", "template": RLS_POLICY_USER}],
    "memory_node_sources": [{"name": "memory_node_sources_user_policy", "template": RLS_POLICY_USER}],
    "memory_derivation_sources": [{"name": "memory_derivation_sources_user_policy", "template": RLS_POLICY_USER}],
    "private_data_leases": [{"name": "private_data_leases_user_policy", "template": RLS_POLICY_USER}],
    "key_model_status": [{"name": "key_model_status_admin_policy", "template": RLS_POLICY_ADMIN}],
    "brief_subscriptions": [{"name": "brief_subscriptions_policy", "template": RLS_POLICY_USER}],
    "conversation_branches": [{"name": "conversation_branches_policy", "template": RLS_POLICY_USER}],
    "user_reminders": [{"name": "user_reminders_policy", "template": RLS_POLICY_USER}],
    "horoscope_subscriptions": [{"name": "horoscope_subscriptions_policy", "template": RLS_POLICY_USER}],
    "tarot_daily_subscriptions": [{"name": "tarot_daily_subscriptions_policy", "template": RLS_POLICY_USER}],
    "user_achievements": [{"name": "user_achievements_policy", "template": RLS_POLICY_USER}],
    "global_settings": [{"name": "global_settings_policy", "template": RLS_POLICY_ADMIN}],
    "inline_boards": [{"name": "inline_boards_policy", "template": RLS_POLICY_ADMIN}],
}

VALID_TABLES = set(RLS_CONFIG.keys())

# Regex for safely validating SQL identifiers (table names) before interpolation
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def quote_ident(ident: str) -> str:
    """Safely quote a PostgreSQL identifier (table or policy name)."""
    return '"' + ident.replace('"', '""') + '"'


async def setup_row_level_security(db_query):
    """Configure Row Level Security for every expected table, failing closed."""
    for table in sorted(VALID_TABLES):
        if not _SAFE_IDENTIFIER_RE.fullmatch(table):
            raise ValueError(f"Unsafe table name in RLS configuration: {table!r}")

        try:
            await db_query(f"ALTER TABLE {quote_ident(table)} ENABLE ROW LEVEL SECURITY;")
            await create_rls_policies(table, db_query)
        except Exception:
            logging.exception("Failed to configure RLS for table %s", table)
            raise


async def create_rls_policies(table_name: str, db_query):
    """Create security policies for a table."""
    if table_name not in VALID_TABLES:
        raise ValueError(f"Invalid table name for RLS policy: {table_name!r}")

    policies = RLS_CONFIG[table_name]
    if not policies:
        raise ValueError(f"No RLS policies configured for table: {table_name!r}")

    # Fetch all existing policies for the table at once to avoid N+1 queries.
    existing_policy_records = await db_query(
        "SELECT policyname FROM pg_policies WHERE schemaname = 'public' AND tablename = $1",
        (table_name,),
    )
    existing_policies = {row["policyname"] for row in existing_policy_records}

    for policy_cfg in policies:
        policy_name = policy_cfg["name"]
        if policy_name in existing_policies:
            continue

        if "sql" in policy_cfg:
            sql = policy_cfg["sql"]
        elif "template" in policy_cfg:
            sql = policy_cfg["template"].format(
                policy_name=quote_ident(policy_name),
                table_name=quote_ident(table_name),
            )
        else:
            raise ValueError(f"Missing SQL or template for RLS policy: {policy_name!r}")

        await db_query(sql)
