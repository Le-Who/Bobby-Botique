-- 063_add_trivia_super_questions.sql
-- Add super_questions column to daily_trivia_puzzles and super_delta/super_correct to daily_trivia_results

ALTER TABLE public.daily_trivia_puzzles
    ADD COLUMN IF NOT EXISTS super_questions JSONB NOT NULL DEFAULT '[]';

ALTER TABLE public.daily_trivia_results
    ADD COLUMN IF NOT EXISTS super_delta   INTEGER DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS super_correct INTEGER DEFAULT NULL;
