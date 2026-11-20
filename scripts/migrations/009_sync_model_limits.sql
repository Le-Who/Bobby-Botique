
INSERT INTO model_configuration (model_name, daily_limit, provider) VALUES
    ('gemini-2.5-flash', 15, 'Google'),
    ('gemini-2.5-flash-latest', 15, 'Google'),
    ('gemini-2.5-flash-lite', 15, 'Google'),
    ('gemini-flash-latest', 15, 'Google'),
    ('gemini-flash-lite-latest', 15, 'Google'),
    ('gemini-3-flash-preview', 15, 'Google')
ON CONFLICT (model_name) DO UPDATE
    SET daily_limit = EXCLUDED.daily_limit, provider = EXCLUDED.provider;
