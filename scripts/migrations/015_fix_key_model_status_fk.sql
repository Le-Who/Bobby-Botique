
ALTER TABLE key_model_status
    DROP CONSTRAINT IF EXISTS key_model_status_key_hash_fkey;

CREATE OR REPLACE FUNCTION check_key_hash_exists()
RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM api_keys WHERE key_hash = NEW.key_hash)
       OR EXISTS (SELECT 1 FROM openrouter_api_keys WHERE key_hash = NEW.key_hash)
    THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'key_hash "%" not found in api_keys or openrouter_api_keys', NEW.key_hash;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_key_model_status_check_key ON key_model_status;
CREATE TRIGGER trg_key_model_status_check_key
    BEFORE INSERT OR UPDATE ON key_model_status
    FOR EACH ROW
    EXECUTE FUNCTION check_key_hash_exists();

DELETE FROM key_model_status
WHERE key_hash NOT IN (
    SELECT key_hash FROM api_keys
    UNION ALL
    SELECT key_hash FROM openrouter_api_keys
);
