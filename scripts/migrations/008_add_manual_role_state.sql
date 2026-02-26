-- Add manual role creation fields to user_state for restart-resilient persistence.
-- These columns allow the bot to remember in-progress manual role creation
-- across restarts/redeployments.

ALTER TABLE user_state
  ADD COLUMN IF NOT EXISTS awaiting_manual_role_title BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS awaiting_manual_role_prompt BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS manual_role_title TEXT DEFAULT '',
  ADD COLUMN IF NOT EXISTS manual_role_prompt TEXT DEFAULT '';
