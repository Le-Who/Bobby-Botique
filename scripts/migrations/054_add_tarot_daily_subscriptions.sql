-- 054_add_tarot_daily_subscriptions.sql
-- Subscription table for the "Карта дня" (Daily Tarot Card) broadcast.
--
-- Design mirrors crocodile_daily_preferences:
--   user_id             FK → users
--   is_subscribed       TRUE = wants daily delivery
--   timezone            IANA tz string for local-time scheduling
--   preferred_local_hour hour (0-23) at which user prefers delivery
--   last_sent_date      last date the card was delivered (prevents re-delivery same day)
--   created_at / updated_at housekeeping

CREATE TABLE IF NOT EXISTS public.tarot_daily_subscriptions (
    user_id              BIGINT PRIMARY KEY REFERENCES public.users(user_id) ON DELETE CASCADE,
    is_subscribed        BOOLEAN NOT NULL DEFAULT FALSE,
    timezone             TEXT NOT NULL DEFAULT 'Europe/Kyiv',
    preferred_local_hour SMALLINT NOT NULL DEFAULT 10 CHECK (preferred_local_hour BETWEEN 0 AND 23),
    last_sent_date       DATE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fast lookup: only active subscribers, ordered by their preferred hour
CREATE INDEX IF NOT EXISTS idx_tarot_daily_subs_delivery
    ON public.tarot_daily_subscriptions (is_subscribed, preferred_local_hour, last_sent_date)
    WHERE is_subscribed = TRUE;

-- Enable RLS (service_role bypasses automatically)
ALTER TABLE public.tarot_daily_subscriptions ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'tarot_daily_subscriptions'
          AND policyname = 'tarot_daily_subs_open'
    ) THEN
        CREATE POLICY tarot_daily_subs_open
            ON public.tarot_daily_subscriptions
            USING (true);   -- bot connects as service role — open policy
    END IF;
END $$;
