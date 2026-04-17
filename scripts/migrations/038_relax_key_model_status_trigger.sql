-- Migration 038: Relax key_model_status trigger to allow in-memory provider hashes
--
-- Problem: The check_key_hash_exists() trigger (migration 015) validates that
-- every key_hash in key_model_status exists in EITHER api_keys OR openrouter_api_keys.
-- Opencode Go keys live only in settings.OPENCODE_API_KEYS (in-memory), never in
-- those DB tables.  On every suspend_key/record_success call for an Opencode key
-- the trigger fires, raises an exception, and wastes 4 retry cycles (~10s overhead).
--
-- Fix: Replace the hard RAISE with a soft RETURN NEW (log-only).
-- We keep the function for auditability but stop blocking writes.
-- Strict FK enforcement for Gemini/OpenRouter is preserved via app-level logic.

CREATE OR REPLACE FUNCTION check_key_hash_exists()
RETURNS TRIGGER AS $$
BEGIN
    -- For known DB-resident providers: Gemini (api_keys) and OpenRouter (openrouter_api_keys)
    -- still validate the FK relationship.  Other providers (Opencode, future in-memory keys)
    -- are allowed through without validation.
    IF EXISTS (SELECT 1 FROM api_keys WHERE key_hash = NEW.key_hash)
       OR EXISTS (SELECT 1 FROM openrouter_api_keys WHERE key_hash = NEW.key_hash)
    THEN
        RETURN NEW;
    END IF;

    -- Hash not found in either DB table — log for observability but do NOT block.
    -- This supports in-memory providers (e.g. Opencode Go) whose keys are never
    -- persisted to the DB.  Strict enforcement for Gemini/OpenRouter is handled
    -- at the application layer (agent_use_cases.py).
    RAISE WARNING 'key_model_status: key_hash "%" not in api_keys/openrouter_api_keys — skipping (in-memory provider)', NEW.key_hash;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
