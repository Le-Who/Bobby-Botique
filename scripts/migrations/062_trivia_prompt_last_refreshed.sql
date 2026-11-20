
ALTER TABLE public.daily_trivia_prompt_messages
    ADD COLUMN IF NOT EXISTS last_refreshed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS daily_trivia_prompt_messages_refresh_idx
    ON public.daily_trivia_prompt_messages (puzzle_date, is_active, last_refreshed_at NULLS FIRST)
    WHERE is_active = TRUE;
