-- Migration 016: Fix check_key_hash_exists() search_path security advisory
--
-- Supabase advisor flagged: "Function public.check_key_hash_exists has a role
-- mutable search_path". Setting search_path = public ensures the function
-- always resolves table names from the public schema, preventing hijack via
-- a malicious schema earlier in the path.

CREATE OR REPLACE FUNCTION check_key_hash_exists()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM api_keys WHERE key_hash = NEW.key_hash)
       OR EXISTS (SELECT 1 FROM openrouter_api_keys WHERE key_hash = NEW.key_hash)
    THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'key_hash "%" not found in api_keys or openrouter_api_keys', NEW.key_hash;
END;
$$;
