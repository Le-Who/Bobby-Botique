-- Revision fencing adds no gameplay changes and preserves every existing result.
ALTER TABLE public.daily_2048_puzzles
    ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.daily_2048_puzzles'::regclass
          AND conname = 'daily_2048_puzzles_revision_positive'
    ) THEN
        ALTER TABLE public.daily_2048_puzzles
            ADD CONSTRAINT daily_2048_puzzles_revision_positive CHECK (revision >= 1);
    END IF;
END $$;
