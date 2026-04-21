-- 039_add_daily_crocodile.sql
-- Wordle-like daily Crocodile mode: global daily puzzle, opt-in delivery,
-- result tracking, leaderboards, and live-edited result messages.

CREATE TABLE IF NOT EXISTS public.crocodile_daily_puzzles (
    puzzle_date DATE PRIMARY KEY,
    target_word TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT 'Разное',
    lang TEXT NOT NULL DEFAULT 'ru',
    hints JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.crocodile_daily_results (
    user_id BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    puzzle_date DATE NOT NULL REFERENCES public.crocodile_daily_puzzles(puzzle_date) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'won', 'lost')),
    attempts JSONB NOT NULL DEFAULT '[]'::jsonb,
    best_score REAL NOT NULL DEFAULT 0,
    used_hints_count INTEGER NOT NULL DEFAULT 0,
    won_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    points INTEGER NOT NULL DEFAULT 0,
    share_grid TEXT NOT NULL DEFAULT '',
    streak_after INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, puzzle_date)
);

CREATE INDEX IF NOT EXISTS idx_croc_daily_results_leaderboard
    ON public.crocodile_daily_results (puzzle_date, points DESC, status, won_at ASC);

CREATE TABLE IF NOT EXISTS public.crocodile_daily_preferences (
    user_id BIGINT PRIMARY KEY REFERENCES public.users(user_id) ON DELETE CASCADE,
    is_subscribed BOOLEAN NOT NULL DEFAULT FALSE,
    timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv',
    preferred_local_hour SMALLINT NOT NULL DEFAULT 13 CHECK (preferred_local_hour BETWEEN 0 AND 23),
    last_sent_puzzle_date DATE,
    discovery_last_sent_at TIMESTAMPTZ,
    discovery_snoozed_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_croc_daily_preferences_delivery
    ON public.crocodile_daily_preferences (is_subscribed, preferred_local_hour, last_sent_puzzle_date);

CREATE INDEX IF NOT EXISTS idx_croc_daily_preferences_discovery
    ON public.crocodile_daily_preferences (is_subscribed, discovery_snoozed_until, discovery_last_sent_at);

CREATE TABLE IF NOT EXISTS public.crocodile_player_activity (
    user_id BIGINT PRIMARY KEY REFERENCES public.users(user_id) ON DELETE CASCADE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    classic_games_started INTEGER NOT NULL DEFAULT 0,
    classic_games_played INTEGER NOT NULL DEFAULT 0,
    daily_games_played INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_croc_player_activity_last_seen
    ON public.crocodile_player_activity (last_seen_at DESC);

CREATE TABLE IF NOT EXISTS public.crocodile_daily_result_messages (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    puzzle_date DATE NOT NULL REFERENCES public.crocodile_daily_puzzles(puzzle_date) ON DELETE CASCADE,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    rendered_hash TEXT NOT NULL DEFAULT '',
    last_edit_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_croc_daily_result_messages_active
    ON public.crocodile_daily_result_messages (puzzle_date, is_active, updated_at DESC);
