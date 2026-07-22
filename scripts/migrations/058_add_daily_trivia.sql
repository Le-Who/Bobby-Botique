-- 058_add_daily_trivia.sql
-- Daily Trivia game: 5 AI-generated trivia questions per day with explanations and score tracking.

CREATE TABLE IF NOT EXISTS public.daily_trivia_puzzles (
    puzzle_date DATE PRIMARY KEY,
    questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'draft',
    prepared_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT daily_trivia_status_check CHECK (status IN ('draft', 'ready', 'disabled'))
);

CREATE TABLE IF NOT EXISTS public.daily_trivia_results (
    user_id BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    puzzle_date DATE NOT NULL REFERENCES public.daily_trivia_puzzles(puzzle_date) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active',
    current_question INTEGER NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    final_score INTEGER NOT NULL DEFAULT 0,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    answers JSONB NOT NULL DEFAULT '[]'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, puzzle_date),
    CONSTRAINT daily_trivia_result_status_check CHECK (status IN ('active', 'completed')),
    CONSTRAINT daily_trivia_result_current_q_check CHECK (current_question >= 0),
    CONSTRAINT daily_trivia_result_correct_check CHECK (correct_count >= 0)
);

CREATE INDEX IF NOT EXISTS daily_trivia_results_leaderboard_idx
    ON public.daily_trivia_results (
        puzzle_date,
        status,
        final_score DESC,
        correct_count DESC,
        elapsed_ms ASC,
        finished_at ASC
    );

CREATE INDEX IF NOT EXISTS daily_trivia_puzzles_status_idx
    ON public.daily_trivia_puzzles (puzzle_date DESC, status, prepared_at DESC NULLS LAST);
