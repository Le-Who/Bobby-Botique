-- 040_daily_crocodile_preparation_assets.sql
-- Extend daily Crocodile puzzles with pre-generated completion assets/readiness.

ALTER TABLE public.crocodile_daily_puzzles
    ADD COLUMN IF NOT EXISTS image_prompt TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS image_file_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS image_model TEXT NOT NULL DEFAULT 'qwen-image',
    ADD COLUMN IF NOT EXISTS prepared_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_croc_daily_puzzles_prepared_at
    ON public.crocodile_daily_puzzles (prepared_at DESC NULLS LAST);
