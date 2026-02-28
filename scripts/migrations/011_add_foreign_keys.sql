-- Migration 011: Add foreign key constraints with ON DELETE CASCADE
-- Applied via Supabase MCP on 2026-02-28
--
-- Pre-cleanup: Removed 403 orphan rows (2 chats, 400 conv_messages, 1 group_message)
-- Added 18 FK constraints across 13 tables
--
-- Design decisions:
--   - ON DELETE CASCADE for all child→parent relationships
--   - ON DELETE SET NULL for nullable owner_user_id columns
--   - Idempotent DO $$ blocks per schema-constraints best practice

-- ── STEP 1: Clean up orphan rows ────────────────────────────────────────────

DELETE FROM chats c WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = c.user_id);
DELETE FROM conversation_messages cm WHERE NOT EXISTS (SELECT 1 FROM conversations cv WHERE cv.id = cm.conversation_id);
DELETE FROM group_messages gms WHERE NOT EXISTS (SELECT 1 FROM group_chats gc WHERE gc.chat_id = gms.chat_id) OR NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = gms.user_id);

-- ── STEP 2: Add FK constraints ──────────────────────────────────────────────

DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_chats_user_id') THEN ALTER TABLE chats ADD CONSTRAINT fk_chats_user_id FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_active_chat_messages_user_id') THEN ALTER TABLE active_chat_messages ADD CONSTRAINT fk_active_chat_messages_user_id FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_conversations_user_id') THEN ALTER TABLE conversations ADD CONSTRAINT fk_conversations_user_id FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_conv_messages_conversation_id') THEN ALTER TABLE conversation_messages ADD CONSTRAINT fk_conv_messages_conversation_id FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_conv_messages_owner_user_id') THEN ALTER TABLE conversation_messages ADD CONSTRAINT fk_conv_messages_owner_user_id FOREIGN KEY (owner_user_id) REFERENCES users(user_id) ON DELETE SET NULL; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_user_roles_user_id') THEN ALTER TABLE user_roles ADD CONSTRAINT fk_user_roles_user_id FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_user_documents_user_id') THEN ALTER TABLE user_documents ADD CONSTRAINT fk_user_documents_user_id FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_user_state_user_id') THEN ALTER TABLE user_state ADD CONSTRAINT fk_user_state_user_id FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_user_metrics_user_id') THEN ALTER TABLE user_metrics ADD CONSTRAINT fk_user_metrics_user_id FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_feedback_user_id') THEN ALTER TABLE feedback ADD CONSTRAINT fk_feedback_user_id FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_group_members_chat_id') THEN ALTER TABLE group_members ADD CONSTRAINT fk_group_members_chat_id FOREIGN KEY (chat_id) REFERENCES group_chats(chat_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_group_members_user_id') THEN ALTER TABLE group_members ADD CONSTRAINT fk_group_members_user_id FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_group_messages_chat_id') THEN ALTER TABLE group_messages ADD CONSTRAINT fk_group_messages_chat_id FOREIGN KEY (chat_id) REFERENCES group_chats(chat_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_group_messages_user_id') THEN ALTER TABLE group_messages ADD CONSTRAINT fk_group_messages_user_id FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_group_messages_owner_user_id') THEN ALTER TABLE group_messages ADD CONSTRAINT fk_group_messages_owner_user_id FOREIGN KEY (owner_user_id) REFERENCES users(user_id) ON DELETE SET NULL; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_key_usage_key_hash') THEN ALTER TABLE key_usage ADD CONSTRAINT fk_key_usage_key_hash FOREIGN KEY (key_hash) REFERENCES api_keys(key_hash) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_or_key_usage_key_hash') THEN ALTER TABLE openrouter_key_usage ADD CONSTRAINT fk_or_key_usage_key_hash FOREIGN KEY (key_hash) REFERENCES openrouter_api_keys(key_hash) ON DELETE CASCADE; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_tav_key_usage_key_hash') THEN ALTER TABLE tavily_key_usage ADD CONSTRAINT fk_tav_key_usage_key_hash FOREIGN KEY (key_hash) REFERENCES tavily_api_keys(key_hash) ON DELETE CASCADE; END IF; END $$;
