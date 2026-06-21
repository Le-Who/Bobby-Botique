-- Migration 053: Fix double-encoded JSONB columns in user_state
--
-- Root cause: save_user_state previously called json.dumps() before passing
-- values to asyncpg with a ::jsonb SQL cast. asyncpg's jsonb codec was already
-- registered on every connection, so the string was stored as a jsonb STRING
-- literal instead of a jsonb OBJECT. This caused 'str' object has no attribute
-- 'get' errors at runtime.
--
-- The write path is now fixed (migration is a data fix for existing rows only).
-- This migration repairs all rows where the column contains a JSON string literal
-- (jsonb_typeof = 'string') instead of a JSON object (jsonb_typeof = 'object').
--
-- Formula: (col #>> '{}') extracts the raw text of the outer string literal,
-- then ::jsonb re-parses it as a proper jsonb value.
--
-- Idempotent: rows already storing a jsonb object are unaffected (WHERE clause
-- guards on jsonb_typeof = 'string').

-- Fix tarot_session
UPDATE user_state
SET tarot_session = (tarot_session #>> '{}')::jsonb
WHERE tarot_session IS NOT NULL
  AND jsonb_typeof(tarot_session) = 'string';

-- Fix generated_role
UPDATE user_state
SET generated_role = (generated_role #>> '{}')::jsonb
WHERE generated_role IS NOT NULL
  AND jsonb_typeof(generated_role) = 'string';

-- Fix role_diaries
UPDATE user_state
SET role_diaries = (role_diaries #>> '{}')::jsonb
WHERE role_diaries IS NOT NULL
  AND jsonb_typeof(role_diaries) = 'string';
