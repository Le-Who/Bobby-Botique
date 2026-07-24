-- 062_trivia_prompt_last_refreshed.sql
-- Add last_refreshed_at to track when a result message was last edited,
-- enabling throttled leaderboard refreshes (no more than once per N minutes).

ALTER TABLE public.daily_trivia_prompt_messages
    ADD COLUMN IF NOT EXISTS last_refreshed_at TIMESTAMPTZ;

-- Index for efficiently finding stale active prompts across all users for a date.
CREATE INDEX IF NOT EXISTS daily_trivia_prompt_messages_refresh_idx
    ON public.daily_trivia_prompt_messages (puzzle_date, is_active, last_refreshed_at NULLS FIRST)
    WHERE is_active = TRUE;
