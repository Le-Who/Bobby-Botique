-- Migration: Remove unused indexes
-- Description: Resolves Supabase linter warning for unused unused_index.

DROP INDEX IF EXISTS idx_conversations_user_updated;
DROP INDEX IF EXISTS idx_messages_conv_created;
DROP INDEX IF EXISTS idx_conversations_user_created;
