ALTER TABLE public.inline_boards ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'inline_boards'
          AND policyname = 'inline_boards_policy'
    ) THEN
        EXECUTE $policy$
            CREATE POLICY inline_boards_policy ON public.inline_boards
            FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true')
        $policy$;
    END IF;
END $$;

ALTER TABLE public.global_settings ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'global_settings'
          AND policyname = 'global_settings_policy'
    ) THEN
        EXECUTE $policy$
            CREATE POLICY global_settings_policy ON public.global_settings
            FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true')
        $policy$;
    END IF;
END $$;
