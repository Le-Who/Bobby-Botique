ALTER TABLE public.crocodile_daily_puzzles
    ADD COLUMN IF NOT EXISTS content_revision INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS generation_token UUID;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='public.crocodile_daily_puzzles'::regclass
                     AND conname='crocodile_daily_content_revision_positive') THEN
        ALTER TABLE public.crocodile_daily_puzzles
            ADD CONSTRAINT crocodile_daily_content_revision_positive CHECK(content_revision >= 1);
    END IF;
END $$;
