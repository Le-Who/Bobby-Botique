-- 048_add_daily_2048.sql
-- Daily 2048 Sprint: one prepared board per day plus first-solution results.

CREATE TABLE IF NOT EXISTS public.daily_2048_puzzles (
    puzzle_date DATE PRIMARY KEY,
    board JSONB NOT NULL DEFAULT '[[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]]'::jsonb,
    goal_type TEXT NOT NULL DEFAULT 'tile',
    goal_value INTEGER NOT NULL DEFAULT 512,
    spawn_sequence JSONB NOT NULL DEFAULT '[]'::jsonb,
    seed TEXT NOT NULL DEFAULT '',
    par_moves INTEGER NOT NULL DEFAULT 72,
    target_seconds INTEGER NOT NULL DEFAULT 240,
    status TEXT NOT NULL DEFAULT 'draft',
    solution_moves TEXT NOT NULL DEFAULT '',
    prepared_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT daily_2048_goal_type_check CHECK (goal_type IN ('tile', 'total')),
    CONSTRAINT daily_2048_status_check CHECK (status IN ('draft', 'ready', 'disabled')),
    CONSTRAINT daily_2048_goal_value_check CHECK (goal_value >= 8),
    CONSTRAINT daily_2048_par_moves_check CHECK (par_moves > 0),
    CONSTRAINT daily_2048_target_seconds_check CHECK (target_seconds >= 30)
);

CREATE TABLE IF NOT EXISTS public.daily_2048_results (
    user_id BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    puzzle_date DATE NOT NULL REFERENCES public.daily_2048_puzzles(puzzle_date) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active',
    board JSONB NOT NULL DEFAULT '[[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]]'::jsonb,
    spawn_index INTEGER NOT NULL DEFAULT 0,
    moves INTEGER NOT NULL DEFAULT 0,
    merge_score INTEGER NOT NULL DEFAULT 0,
    final_score INTEGER NOT NULL DEFAULT 0,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    won_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    recordable BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, puzzle_date),
    CONSTRAINT daily_2048_result_status_check CHECK (status IN ('active', 'won', 'lost')),
    CONSTRAINT daily_2048_result_moves_check CHECK (moves >= 0),
    CONSTRAINT daily_2048_result_elapsed_check CHECK (elapsed_ms >= 0)
);

CREATE INDEX IF NOT EXISTS daily_2048_results_leaderboard_idx
    ON public.daily_2048_results (
        puzzle_date,
        recordable,
        status,
        final_score DESC,
        moves ASC,
        elapsed_ms ASC,
        won_at ASC
    );

CREATE INDEX IF NOT EXISTS daily_2048_puzzles_status_idx
    ON public.daily_2048_puzzles (puzzle_date DESC, status, prepared_at DESC NULLS LAST);

INSERT INTO public.global_settings (key_name, value_data)
VALUES ('daily_game_mode', 'crocodile')
ON CONFLICT (key_name) DO NOTHING;
