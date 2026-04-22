-- 043_daily_crocodile_dual_track.sql
-- Introduce independent easy/hard daily Crocodile tracks while preserving
-- date-level delivery/result message flows.

CREATE TABLE IF NOT EXISTS public.crocodile_daily_days (
    puzzle_date DATE PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO public.crocodile_daily_days (puzzle_date, created_at)
SELECT puzzle_date, COALESCE(MIN(created_at), NOW())
FROM public.crocodile_daily_puzzles
GROUP BY puzzle_date
ON CONFLICT (puzzle_date) DO NOTHING;

ALTER TABLE public.crocodile_daily_preferences
    ADD COLUMN IF NOT EXISTS last_sent_local_date DATE;

UPDATE public.crocodile_daily_preferences
SET last_sent_local_date = last_sent_puzzle_date
WHERE last_sent_local_date IS NULL
  AND last_sent_puzzle_date IS NOT NULL;

ALTER TABLE public.crocodile_daily_puzzles
    ADD COLUMN IF NOT EXISTS difficulty TEXT NOT NULL DEFAULT 'easy';

UPDATE public.crocodile_daily_puzzles
SET difficulty = 'easy'
WHERE COALESCE(difficulty, '') = '';

ALTER TABLE public.crocodile_daily_puzzles
    DROP CONSTRAINT IF EXISTS crocodile_daily_puzzles_difficulty_check;

ALTER TABLE public.crocodile_daily_puzzles
    ADD CONSTRAINT crocodile_daily_puzzles_difficulty_check
    CHECK (difficulty IN ('easy', 'hard'));

ALTER TABLE public.crocodile_daily_results
    ADD COLUMN IF NOT EXISTS difficulty TEXT NOT NULL DEFAULT 'easy';

UPDATE public.crocodile_daily_results
SET difficulty = 'easy'
WHERE COALESCE(difficulty, '') = '';

ALTER TABLE public.crocodile_daily_results
    DROP CONSTRAINT IF EXISTS crocodile_daily_results_difficulty_check;

ALTER TABLE public.crocodile_daily_results
    ADD CONSTRAINT crocodile_daily_results_difficulty_check
    CHECK (difficulty IN ('easy', 'hard'));

ALTER TABLE public.crocodile_daily_results
    DROP CONSTRAINT IF EXISTS crocodile_daily_results_puzzle_date_fkey;

ALTER TABLE public.crocodile_daily_result_messages
    DROP CONSTRAINT IF EXISTS crocodile_daily_result_messages_puzzle_date_fkey;

ALTER TABLE public.crocodile_daily_prompt_messages
    DROP CONSTRAINT IF EXISTS crocodile_daily_prompt_messages_puzzle_date_fkey;

ALTER TABLE public.crocodile_daily_puzzles
    DROP CONSTRAINT IF EXISTS crocodile_daily_puzzles_pkey;

ALTER TABLE public.crocodile_daily_results
    DROP CONSTRAINT IF EXISTS crocodile_daily_results_pkey;

ALTER TABLE public.crocodile_daily_puzzles
    ADD CONSTRAINT crocodile_daily_puzzles_pkey PRIMARY KEY (puzzle_date, difficulty);

ALTER TABLE public.crocodile_daily_results
    ADD CONSTRAINT crocodile_daily_results_pkey PRIMARY KEY (user_id, puzzle_date, difficulty);

ALTER TABLE public.crocodile_daily_puzzles
    DROP CONSTRAINT IF EXISTS crocodile_daily_puzzles_puzzle_date_fkey;

ALTER TABLE public.crocodile_daily_puzzles
    ADD CONSTRAINT crocodile_daily_puzzles_puzzle_date_fkey
    FOREIGN KEY (puzzle_date) REFERENCES public.crocodile_daily_days(puzzle_date) ON DELETE CASCADE;

ALTER TABLE public.crocodile_daily_results
    DROP CONSTRAINT IF EXISTS crocodile_daily_results_puzzle_fkey;

ALTER TABLE public.crocodile_daily_results
    ADD CONSTRAINT crocodile_daily_results_puzzle_fkey
    FOREIGN KEY (puzzle_date, difficulty)
    REFERENCES public.crocodile_daily_puzzles(puzzle_date, difficulty)
    ON DELETE CASCADE;

ALTER TABLE public.crocodile_daily_result_messages
    ADD CONSTRAINT crocodile_daily_result_messages_puzzle_date_fkey
    FOREIGN KEY (puzzle_date) REFERENCES public.crocodile_daily_days(puzzle_date) ON DELETE CASCADE;

ALTER TABLE public.crocodile_daily_prompt_messages
    ADD CONSTRAINT crocodile_daily_prompt_messages_puzzle_date_fkey
    FOREIGN KEY (puzzle_date) REFERENCES public.crocodile_daily_days(puzzle_date) ON DELETE CASCADE;

DROP INDEX IF EXISTS idx_croc_daily_results_leaderboard;

CREATE INDEX IF NOT EXISTS idx_croc_daily_results_leaderboard
    ON public.crocodile_daily_results (puzzle_date, difficulty, points DESC, status, won_at ASC);

CREATE INDEX IF NOT EXISTS idx_croc_daily_puzzles_date_prepared
    ON public.crocodile_daily_puzzles (puzzle_date DESC, difficulty, prepared_at DESC NULLS LAST);

DROP INDEX IF EXISTS idx_croc_daily_preferences_delivery;

CREATE INDEX IF NOT EXISTS idx_croc_daily_preferences_delivery
    ON public.crocodile_daily_preferences (
        is_subscribed,
        preferred_local_hour,
        last_sent_puzzle_date,
        last_sent_local_date
    );
