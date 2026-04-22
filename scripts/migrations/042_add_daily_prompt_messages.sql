-- 042_add_daily_prompt_messages.sql
-- Track the scheduled delivery photo message per user+day so we can swap
-- the placeholder art → real art when the player finishes the game.
-- Also adds message_type to result_messages so the refresh loop knows
-- whether to use edit_message_caption or edit_message_text.

CREATE TABLE IF NOT EXISTS public.crocodile_daily_prompt_messages (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    puzzle_date DATE   NOT NULL REFERENCES public.crocodile_daily_puzzles(puzzle_date) ON DELETE CASCADE,
    chat_id     BIGINT NOT NULL,
    message_id  BIGINT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- One prompt message per user per day; on re-delivery overwrite coordinates.
    UNIQUE (user_id, puzzle_date)
);

CREATE INDEX IF NOT EXISTS idx_croc_daily_prompt_messages_lookup
    ON public.crocodile_daily_prompt_messages (user_id, puzzle_date, is_active);

-- Add message_type so the leaderboard refresh loop picks the right edit API.
-- 'text'  → edit_message_text   (plain text result messages)
-- 'photo' → edit_message_caption (swapped art messages)
ALTER TABLE public.crocodile_daily_result_messages
    ADD COLUMN IF NOT EXISTS message_type TEXT NOT NULL DEFAULT 'text';
