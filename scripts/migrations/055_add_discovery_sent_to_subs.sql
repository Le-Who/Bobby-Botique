
ALTER TABLE horoscope_subscriptions
    ADD COLUMN IF NOT EXISTS discovery_last_sent_at TIMESTAMPTZ;

ALTER TABLE public.tarot_daily_subscriptions
    ADD COLUMN IF NOT EXISTS discovery_last_sent_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_horoscope_subs_discovery_sent
    ON horoscope_subscriptions (discovery_last_sent_at)
    WHERE discovery_last_sent_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tarot_daily_subs_discovery_sent
    ON public.tarot_daily_subscriptions (discovery_last_sent_at)
    WHERE discovery_last_sent_at IS NOT NULL;
