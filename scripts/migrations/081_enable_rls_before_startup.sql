-- A migration-only setup must not expose application tables while waiting for
-- the Python startup path to provision its normal policies. Existing policies,
-- owner access and BYPASSRLS behavior are intentionally left unchanged.
DO $migration$
DECLARE
    target_table TEXT;
    rls_enabled BOOLEAN;
BEGIN
    FOREACH target_table IN ARRAY ARRAY[
        'active_chat_messages', 'api_keys', 'brief_subscriptions',
        'broadcast_batches', 'broadcast_delivery_attempts', 'broadcast_journal_coverage',
        'broadcast_recipients', 'chats', 'conversation_branches',
        'conversation_messages', 'conversations', 'crocodile_daily_days',
        'crocodile_daily_preferences', 'crocodile_daily_prompt_messages',
        'crocodile_daily_puzzles', 'crocodile_daily_result_messages',
        'crocodile_daily_results', 'crocodile_player_activity',
        'daily_2048_prompt_messages', 'daily_2048_puzzles', 'daily_2048_results',
        'daily_trivia_facts', 'daily_trivia_preferences', 'daily_trivia_prompt_messages',
        'daily_trivia_puzzle_revisions', 'daily_trivia_puzzles',
        'daily_trivia_question_occurrences', 'daily_trivia_question_variants',
        'daily_trivia_results', 'daily_trivia_super_results', 'daily_trivia_used_keys',
        'error_logs', 'feedback', 'global_settings', 'group_chats', 'group_members',
        'group_messages', 'horoscope_subscriptions', 'inline_boards', 'key_model_status',
        'key_usage', 'long_term_memory', 'memory_derivation_sources', 'memory_edge_sources',
        'memory_edges', 'memory_node_sources', 'memory_nodes', 'metrics',
        'model_configuration', 'natal_reports', 'openrouter_api_keys',
        'openrouter_key_usage', 'private_data_leases', 'roles', 'schema_migrations',
        'tarot_daily_generation', 'tarot_daily_readings', 'tarot_daily_subscriptions',
        'tavily_api_keys', 'tavily_key_usage', 'traffic_accounting_epoch', 'traffic_daily',
        'user_achievements', 'user_documents', 'user_metrics', 'user_reminders',
        'user_roles', 'user_state', 'users'
    ] LOOP
        SELECT c.relrowsecurity INTO rls_enabled
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = target_table AND c.relkind IN ('r', 'p');

        IF NOT FOUND THEN
            RAISE EXCEPTION 'Required application table public.% is missing', target_table;
        END IF;
        IF NOT rls_enabled THEN
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', target_table);
        END IF;
    END LOOP;
END
$migration$;
