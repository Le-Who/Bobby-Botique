
CREATE TABLE IF NOT EXISTS public.horoscope_subscriptions (
    user_id             BIGINT PRIMARY KEY REFERENCES public.users(user_id) ON DELETE CASCADE,
    sign                TEXT NOT NULL DEFAULT 'aries',
    time_today          TIME,
    time_tomorrow       TIME,
    utc_offset          INTEGER NOT NULL DEFAULT 3,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    last_today_sent     TIMESTAMP WITH TIME ZONE,
    last_tomorrow_sent  TIMESTAMP WITH TIME ZONE,
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_horoscope_subs_active
    ON public.horoscope_subscriptions (is_active)
    WHERE is_active = TRUE;

ALTER TABLE public.horoscope_subscriptions ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'horoscope_subscriptions'
          AND schemaname = 'public'
          AND policyname = 'horoscope_subscriptions_policy'
    ) THEN
        CREATE POLICY horoscope_subscriptions_policy
            ON public.horoscope_subscriptions
            FOR ALL
            USING (
                user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
                OR current_setting('app.is_admin', true) = 'true'
            )
            WITH CHECK (
                user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
                OR current_setting('app.is_admin', true) = 'true'
            );
    END IF;
END $$;
