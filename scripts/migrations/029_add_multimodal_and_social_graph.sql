-- Migration 029: Add multimodal support to memory nodes
-- Purpose: Stores Telegram file_id on memory nodes for image/audio/document retrieval.
--          When a matching node has a file_id, the bot can re-send the original media.
--
-- Idempotency: All statements guarded by IF NOT EXISTS / conditional checks.

DO $$ BEGIN
    -- Step 1: Add file_id column for Telegram file references
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_nodes'
          AND column_name  = 'file_id'
    ) THEN
        ALTER TABLE memory_nodes
            ADD COLUMN file_id TEXT DEFAULT NULL;

        COMMENT ON COLUMN memory_nodes.file_id IS
            'Telegram file_id for media retrieval (images, audio, documents)';
    END IF;

    -- Step 2: Add file_type to distinguish media types
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_nodes'
          AND column_name  = 'file_type'
    ) THEN
        ALTER TABLE memory_nodes
            ADD COLUMN file_type TEXT DEFAULT NULL;

        COMMENT ON COLUMN memory_nodes.file_type IS
            'Media type: photo, audio, voice, document, video, video_note';
    END IF;

    -- Step 3: Add chat_id for group chat memory isolation (Phase 3 prep)
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_nodes'
          AND column_name  = 'chat_id'
    ) THEN
        ALTER TABLE memory_nodes
            ADD COLUMN chat_id BIGINT DEFAULT NULL;

        COMMENT ON COLUMN memory_nodes.chat_id IS
            'Telegram chat_id for group memory isolation (NULL = private chat)';
    END IF;

    -- Step 4: Add actor_user_id for social graph attribution (Phase 3 prep)
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_nodes'
          AND column_name  = 'actor_user_id'
    ) THEN
        ALTER TABLE memory_nodes
            ADD COLUMN actor_user_id BIGINT DEFAULT NULL;

        COMMENT ON COLUMN memory_nodes.actor_user_id IS
            'Telegram user_id of the actual speaker (for group chat social graph)';
    END IF;
END $$;

-- Step 5: Add chat_id and actor_user_id to memory_edges for group isolation
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_edges'
          AND column_name  = 'chat_id'
    ) THEN
        ALTER TABLE memory_edges
            ADD COLUMN chat_id BIGINT DEFAULT NULL;

        COMMENT ON COLUMN memory_edges.chat_id IS
            'Telegram chat_id for group memory isolation (NULL = private chat)';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_edges'
          AND column_name  = 'actor_user_id'
    ) THEN
        ALTER TABLE memory_edges
            ADD COLUMN actor_user_id BIGINT DEFAULT NULL;

        COMMENT ON COLUMN memory_edges.actor_user_id IS
            'Telegram user_id of the speaker who caused this edge';
    END IF;
END $$;

-- Step 6: Index for multimodal retrieval (find nodes with attached media)
CREATE INDEX IF NOT EXISTS idx_memory_nodes_file_id
    ON memory_nodes (user_id, file_type)
    WHERE file_id IS NOT NULL;

-- Step 7: Index for group chat memory isolation
CREATE INDEX IF NOT EXISTS idx_memory_nodes_chat_id
    ON memory_nodes (chat_id, user_id)
    WHERE chat_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_edges_chat_id
    ON memory_edges (chat_id, user_id)
    WHERE chat_id IS NOT NULL;

-- Step 8: Add is_public flag for group chat privacy isolation
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_edges'
          AND column_name  = 'is_public'
    ) THEN
        ALTER TABLE memory_edges
            ADD COLUMN is_public BOOLEAN DEFAULT TRUE;

        COMMENT ON COLUMN memory_edges.is_public IS
            'Whether this edge is visible to all group members (TRUE) or only the actor (FALSE)';
    END IF;
END $$;
