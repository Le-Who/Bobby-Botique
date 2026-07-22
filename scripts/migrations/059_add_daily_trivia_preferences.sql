-- Migration 059: Add Daily Trivia preferences table
-- Stores subscriber state and last delivery dates per user

CREATE TABLE IF NOT EXISTS public.daily_trivia_preferences (
    user_id BIGINT PRIMARY KEY REFERENCES public.users(user_id) ON DELETE CASCADE,
    is_subscribed BOOLEAN NOT NULL DEFAULT TRUE,
    timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv',
    preferred_local_hour SMALLINT NOT NULL DEFAULT 13 CHECK (preferred_local_hour BETWEEN 0 AND 23),
    last_sent_puzzle_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trivia_daily_preferences_delivery
    ON public.daily_trivia_preferences (is_subscribed, preferred_local_hour, last_sent_puzzle_date);
