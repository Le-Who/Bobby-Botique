-- Migration 060: Add daily_trivia_used_keys table for tracking used question topics/facts
CREATE TABLE IF NOT EXISTS public.daily_trivia_used_keys (
    id BIGSERIAL PRIMARY KEY,
    object_norm TEXT NOT NULL,
    subobject_norm TEXT NOT NULL,
    used_at DATE NOT NULL DEFAULT CURRENT_DATE,
    CONSTRAINT daily_trivia_used_keys_unique UNIQUE (object_norm, subobject_norm)
);

CREATE INDEX IF NOT EXISTS daily_trivia_used_keys_used_at_idx
    ON public.daily_trivia_used_keys (used_at DESC);
