
ALTER TABLE public.daily_trivia_puzzles
    ADD COLUMN IF NOT EXISTS super_questions JSONB NOT NULL DEFAULT '[]';

ALTER TABLE public.daily_trivia_results
    ADD COLUMN IF NOT EXISTS super_delta   INTEGER DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS super_correct INTEGER DEFAULT NULL;
