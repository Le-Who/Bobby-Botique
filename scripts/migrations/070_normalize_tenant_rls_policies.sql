-- Normalize tenant policies created by older migrations.
-- This is deliberately raw-SQL idempotent: every policy is dropped before it
-- is recreated, and the Supabase-only service_role policy is conditional.

ALTER TABLE public.key_model_status ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.brief_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conversation_branches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_reminders ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.horoscope_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tarot_daily_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_achievements ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS key_model_status_admin ON public.key_model_status;
DROP POLICY IF EXISTS key_model_status_admin_policy ON public.key_model_status;
DROP POLICY IF EXISTS key_model_status_service_role ON public.key_model_status;
CREATE POLICY key_model_status_admin_policy ON public.key_model_status
    FOR ALL
    USING (current_setting('app.is_admin', true) = 'true')
    WITH CHECK (current_setting('app.is_admin', true) = 'true');

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE POLICY key_model_status_service_role ON public.key_model_status
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
END
$$;

DROP POLICY IF EXISTS brief_subscriptions_user_policy ON public.brief_subscriptions;
DROP POLICY IF EXISTS brief_subscriptions_admin_policy ON public.brief_subscriptions;
DROP POLICY IF EXISTS brief_subscriptions_policy ON public.brief_subscriptions;
CREATE POLICY brief_subscriptions_policy ON public.brief_subscriptions
    FOR ALL
    USING (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    )
    WITH CHECK (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    );

DROP POLICY IF EXISTS branches_user_policy ON public.conversation_branches;
DROP POLICY IF EXISTS branches_service_bypass ON public.conversation_branches;
DROP POLICY IF EXISTS conversation_branches_policy ON public.conversation_branches;
CREATE POLICY conversation_branches_policy ON public.conversation_branches
    FOR ALL
    USING (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    )
    WITH CHECK (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    );

DROP POLICY IF EXISTS reminders_user_policy ON public.user_reminders;
DROP POLICY IF EXISTS reminders_service_bypass ON public.user_reminders;
DROP POLICY IF EXISTS user_reminders_policy ON public.user_reminders;
CREATE POLICY user_reminders_policy ON public.user_reminders
    FOR ALL
    USING (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    )
    WITH CHECK (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    );

DROP POLICY IF EXISTS horoscope_subs_own ON public.horoscope_subscriptions;
DROP POLICY IF EXISTS horoscope_subscriptions_policy ON public.horoscope_subscriptions;
CREATE POLICY horoscope_subscriptions_policy ON public.horoscope_subscriptions
    FOR ALL
    USING (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    )
    WITH CHECK (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    );

DROP POLICY IF EXISTS tarot_daily_subs_open ON public.tarot_daily_subscriptions;
DROP POLICY IF EXISTS tarot_daily_subscriptions_policy ON public.tarot_daily_subscriptions;
CREATE POLICY tarot_daily_subscriptions_policy ON public.tarot_daily_subscriptions
    FOR ALL
    USING (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    )
    WITH CHECK (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    );

DROP POLICY IF EXISTS user_achievements_policy ON public.user_achievements;
CREATE POLICY user_achievements_policy ON public.user_achievements
    FOR ALL
    USING (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    )
    WITH CHECK (
        user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        OR current_setting('app.is_admin', true) = 'true'
    );
