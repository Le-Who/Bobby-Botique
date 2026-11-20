
CREATE OR REPLACE FUNCTION check_key_hash_exists()
RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM api_keys WHERE key_hash = NEW.key_hash)
       OR EXISTS (SELECT 1 FROM openrouter_api_keys WHERE key_hash = NEW.key_hash)
    THEN
        RETURN NEW;
    END IF;

    RAISE WARNING 'key_model_status: key_hash "%" not in api_keys/openrouter_api_keys — skipping (in-memory provider)', NEW.key_hash;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
