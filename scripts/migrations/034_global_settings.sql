-- Migration 034: global_settings key-value store
-- Stores bot-wide runtime configuration values that can be changed
-- at runtime by admins without restarting the container.

CREATE TABLE IF NOT EXISTS global_settings (
    key_name   TEXT        PRIMARY KEY,
    value_data TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Seed initial thinking level from a sensible default.
-- Admins can override at runtime with /set_inline_thinking.
INSERT INTO global_settings (key_name, value_data)
VALUES ('inline_thinking_level', 'low')
ON CONFLICT (key_name) DO NOTHING;
