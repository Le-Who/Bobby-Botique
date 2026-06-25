-- 055_add_discovery_sent_to_subs.sql
-- Track when users received a "discovery offer" (subscription invite message)
-- for Horoscope and Tarot channels, mirroring the existing
-- discovery_last_sent_at field already present in crocodile_daily_preferences.
--
-- discovery_last_sent_at  — timestamp of the most recent offer sent.
--                           NULL = never offered.
--                           Used in /admin_daily Offer History UI and for
--                           "never offered" filter in manual offer send flow.

-- horoscope_subscriptions
ALTER TABLE horoscope_subscriptions
    ADD COLUMN IF NOT EXISTS discovery_last_sent_at TIMESTAMPTZ;

-- tarot_daily_subscriptions
ALTER TABLE public.tarot_daily_subscriptions
    ADD COLUMN IF NOT EXISTS discovery_last_sent_at TIMESTAMPTZ;

-- Indexes for efficient filtering in the admin offer-history query
CREATE INDEX IF NOT EXISTS idx_horoscope_subs_discovery_sent
    ON horoscope_subscriptions (discovery_last_sent_at)
    WHERE discovery_last_sent_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tarot_daily_subs_discovery_sent
    ON public.tarot_daily_subscriptions (discovery_last_sent_at)
    WHERE discovery_last_sent_at IS NOT NULL;
