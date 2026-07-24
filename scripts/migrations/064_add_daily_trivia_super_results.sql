-- 064_add_daily_trivia_super_results.sql
-- Create daily_trivia_super_results table for tracking super game attempts

CREATE TABLE IF NOT EXISTS public.daily_trivia_super_results (
    user_id       BIGINT   NOT NULL,
    puzzle_date   DATE     NOT NULL,
    status        TEXT     NOT NULL DEFAULT 'active',
    answers       JSONB    NOT NULL DEFAULT '[]',
    delta_score   INTEGER  NOT NULL DEFAULT 0,
    correct_count INTEGER  NOT NULL DEFAULT 0,
    elapsed_ms    BIGINT   NOT NULL DEFAULT 0,
    started_at    TIMESTAMPTZ DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    CONSTRAINT pk_super_results PRIMARY KEY (user_id, puzzle_date),
    CONSTRAINT super_results_status_check CHECK (status IN ('active', 'completed'))
);
