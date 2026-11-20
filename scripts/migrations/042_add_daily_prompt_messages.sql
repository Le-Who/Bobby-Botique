
CREATE TABLE IF NOT EXISTS public.crocodile_daily_prompt_messages (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    puzzle_date DATE   NOT NULL REFERENCES public.crocodile_daily_puzzles(puzzle_date) ON DELETE CASCADE,
    chat_id     BIGINT NOT NULL,
    message_id  BIGINT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, puzzle_date)
);

CREATE INDEX IF NOT EXISTS idx_croc_daily_prompt_messages_lookup
    ON public.crocodile_daily_prompt_messages (user_id, puzzle_date, is_active);

ALTER TABLE public.crocodile_daily_result_messages
    ADD COLUMN IF NOT EXISTS message_type TEXT NOT NULL DEFAULT 'text';
