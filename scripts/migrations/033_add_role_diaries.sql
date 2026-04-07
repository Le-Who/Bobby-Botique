-- Migration 033: Add role diaries to user_state
-- Purpose: MemPalace persistent role-specific diaries.
--          Each role accumulates session insights (key learnings, preferences,
--          style observations) as a JSONB dict keyed by role_id.
--          This enables context continuity across sessions for the same persona.
--
-- Idempotency: Guarded by IF NOT EXISTS check.

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'user_state'
          AND column_name = 'role_diaries'
    ) THEN
        ALTER TABLE user_state ADD COLUMN role_diaries JSONB DEFAULT '{}';
        COMMENT ON COLUMN user_state.role_diaries IS
            'MemPalace role diaries: {role_id: [entry1, entry2, ...]}';
    END IF;
END $$;
