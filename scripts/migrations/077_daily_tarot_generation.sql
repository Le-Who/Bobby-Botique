-- Global, non-personal generation metadata, matching the shared Tarot cache scope.
-- No existing reading, privilege or tenant policy is changed.
CREATE TABLE IF NOT EXISTS public.tarot_daily_generation (
    reading_date DATE NOT NULL,
    language TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    token UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (reading_date, language)
);
