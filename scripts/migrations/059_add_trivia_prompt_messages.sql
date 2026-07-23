-- 059_add_trivia_prompt_messages.sql
-- Track the Telegram message sent to a user with the daily trivia invite so we
-- can edit it in-place to show the result after they finish, mirroring the
-- Daily Crocodile and 2048 Sprint behaviour.

CREATE TABLE IF NOT EXISTS public.daily_trivia_prompt_messages (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    puzzle_date DATE NOT NULL,
    chat_id     BIGINT NOT NULL,
    message_id  BIGINT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT daily_trivia_prompt_messages_uq UNIQUE (user_id, puzzle_date)
);

CREATE INDEX IF NOT EXISTS daily_trivia_prompt_messages_lookup_idx
    ON public.daily_trivia_prompt_messages (user_id, puzzle_date, is_active);
