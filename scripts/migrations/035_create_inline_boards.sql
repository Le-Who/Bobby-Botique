-- 035_create_inline_boards.sql
-- Persistent storage for Collaborative AI-Notes (Topic Aggregator boards).
--
-- Key design decisions:
--   * inline_msg_id is from ChosenInlineResult and is the primary lookup key.
--   * chat_id / message_id are populated lazily on the first callback press
--     (board_link:pending) because ChosenInlineResult does not expose them.
--   * entries are stored as JSONB array: [{user, text, ts}].
--   * entries_since_last_synthesis tracks new entries since the last LLM run,
--     enabling the "only synthesize if there are new inputs" debounce logic.

CREATE TABLE IF NOT EXISTS inline_boards (
    id                          SERIAL PRIMARY KEY,
    inline_msg_id               TEXT        NOT NULL UNIQUE,  -- from ChosenInlineResult
    chat_id                     BIGINT,                       -- linked on first callback
    message_id                  BIGINT,                       -- linked on first callback
    topic                       TEXT        NOT NULL,
    creator_id                  BIGINT      NOT NULL,
    entries                     JSONB       NOT NULL DEFAULT '[]',
    entries_since_last_synthesis INT         NOT NULL DEFAULT 0,
    last_summary                TEXT        NOT NULL DEFAULT '',
    closed                      BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fast lookup by inline_msg_id (primary use case)
CREATE INDEX IF NOT EXISTS idx_inline_boards_inline_msg
    ON inline_boards (inline_msg_id);

-- Fast lookup by chat + message (for reply routing)
CREATE INDEX IF NOT EXISTS idx_inline_boards_chat_msg
    ON inline_boards (chat_id, message_id)
    WHERE chat_id IS NOT NULL;

-- Housekeeping: find boards older than 24h for expiry sweeps
CREATE INDEX IF NOT EXISTS idx_inline_boards_created_at
    ON inline_boards (created_at);
