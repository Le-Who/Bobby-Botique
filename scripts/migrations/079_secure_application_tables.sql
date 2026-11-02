-- These tables are used by the bot's direct PostgreSQL connection. They are
-- not part of a Supabase Data API, whose anon/authenticated roles must not be
-- able to read or modify their contents. Keep owner/service-role RLS bypass
-- unchanged; the application currently uses one migration/runtime DSN.
DO $migration$
DECLARE
    tenant_tables TEXT[] := ARRAY[
        'natal_reports',
        'crocodile_daily_preferences',
        'crocodile_daily_results',
        'crocodile_daily_result_messages',
        'crocodile_daily_prompt_messages',
        'crocodile_player_activity',
        'daily_2048_results',
        'daily_2048_prompt_messages',
        'daily_trivia_preferences',
        'daily_trivia_results',
        'daily_trivia_super_results',
        'daily_trivia_prompt_messages'
    ];
    admin_tables TEXT[] := ARRAY[
        'crocodile_daily_days',
        'crocodile_daily_puzzles',
        'daily_2048_puzzles',
        'daily_trivia_puzzles',
        'daily_trivia_facts',
        'daily_trivia_question_variants',
        'daily_trivia_puzzle_revisions',
        'daily_trivia_question_occurrences',
        'daily_trivia_used_keys',
        'tarot_daily_readings',
        'tarot_daily_generation'
    ];
    tenant_check TEXT := $policy$
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    $policy$;
    admin_check TEXT := $policy$
        (SELECT current_setting('app.is_admin', true)) = 'true'
    $policy$;
    target_table TEXT;
    target_role TEXT;
    policy_check TEXT;
BEGIN
    FOREACH target_table IN ARRAY tenant_tables || admin_tables LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', target_table);
        EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM PUBLIC', target_table);

        -- Plain PostgreSQL installations need not have Supabase's API roles.
        FOREACH target_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = target_role) THEN
                EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM %I', target_table, target_role);
            END IF;
        END LOOP;

        IF target_table = ANY(tenant_tables) THEN
            policy_check := tenant_check;
        ELSE
            policy_check := admin_check;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = target_table
              AND policyname = target_table || '_policy'
        ) THEN
            EXECUTE format(
                'CREATE POLICY %I ON public.%I FOR ALL USING (%s) WITH CHECK (%s)',
                target_table || '_policy', target_table, policy_check, policy_check
            );
        END IF;
    END LOOP;

    -- The roles catalog intentionally permits reads to database users through
    -- RLS, but its prompts must not be exposed through Supabase's API roles.
    REVOKE ALL PRIVILEGES ON TABLE public.roles FROM PUBLIC;
    FOREACH target_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = target_role) THEN
            EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.roles FROM %I', target_role);
        END IF;
    END LOOP;
END
$migration$;
