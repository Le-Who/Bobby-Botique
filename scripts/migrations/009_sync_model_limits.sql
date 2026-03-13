-- Migration: Sync model_configuration with current free-tier limits (15 RPD)
-- Description: Updates stale daily_limit values and adds missing models.
-- Idempotent: UPSERT only, no destructive DELETE.

INSERT INTO model_configuration (model_name, daily_limit, provider) VALUES
    ('gemini-2.5-flash', 15, 'Google'),
    ('gemini-2.5-flash-latest', 15, 'Google'),
    ('gemini-2.5-flash-lite', 15, 'Google'),
    ('gemini-flash-latest', 15, 'Google'),
    ('gemini-flash-lite-latest', 15, 'Google'),
    ('gemini-3-flash-preview', 15, 'Google')
ON CONFLICT (model_name) DO UPDATE
    SET daily_limit = EXCLUDED.daily_limit, provider = EXCLUDED.provider;

-- NOTE: Removed destructive DELETE that wiped any models added after this
-- migration. Model cleanup should be done manually or via a dedicated migration.
