
CREATE TABLE IF NOT EXISTS global_settings (
    key_name   TEXT        PRIMARY KEY,
    value_data TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO global_settings (key_name, value_data)
VALUES ('inline_thinking_level', 'low')
ON CONFLICT (key_name) DO NOTHING;
