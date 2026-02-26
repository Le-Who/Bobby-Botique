-- Migration: Sync model_configuration with current free-tier limits (15 RPD)
-- Description: Updates stale daily_limit values and adds missing models.

INSERT INTO model_configuration (model_name, daily_limit, provider) VALUES
    ('gemini-2.5-flash', 15, 'Google'),
    ('gemini-2.5-flash-latest', 15, 'Google'),
    ('gemini-2.5-flash-lite', 15, 'Google'),
    ('gemini-flash-latest', 15, 'Google'),
    ('gemini-flash-lite-latest', 15, 'Google'),
    ('gemini-3-flash-preview', 15, 'Google')
ON CONFLICT (model_name) DO UPDATE
    SET daily_limit = EXCLUDED.daily_limit, provider = EXCLUDED.provider;

-- Remove stale models no longer in the active config
DELETE FROM model_configuration
WHERE model_name NOT IN (
    'gemini-2.5-flash',
    'gemini-2.5-flash-latest',
    'gemini-2.5-flash-lite',
    'gemini-flash-latest',
    'gemini-flash-lite-latest',
    'gemini-3-flash-preview'
);
