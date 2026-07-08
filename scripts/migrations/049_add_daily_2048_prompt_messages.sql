-- 049_add_daily_2048_prompt_messages.sql
-- Track Daily 2048 invitation messages so completion can edit the original
-- cover prompt into the result instead of sending a separate message.

CREATE TABLE IF NOT EXISTS public.daily_2048_prompt_messages (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    puzzle_date DATE   NOT NULL REFERENCES public.daily_2048_puzzles(puzzle_date) ON DELETE CASCADE,
    chat_id     BIGINT NOT NULL,
    message_id  BIGINT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, puzzle_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_2048_prompt_messages_lookup
    ON public.daily_2048_prompt_messages (user_id, puzzle_date, is_active);
