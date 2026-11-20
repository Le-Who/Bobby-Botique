
CREATE TABLE IF NOT EXISTS inline_boards (
    id                          SERIAL PRIMARY KEY,
    inline_msg_id               TEXT        NOT NULL UNIQUE,
    chat_id                     BIGINT,
    message_id                  BIGINT,
    topic                       TEXT        NOT NULL,
    creator_id                  BIGINT      NOT NULL,
    entries                     JSONB       NOT NULL DEFAULT '[]',
    entries_since_last_synthesis INT         NOT NULL DEFAULT 0,
    last_summary                TEXT        NOT NULL DEFAULT '',
    closed                      BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inline_boards_inline_msg
    ON inline_boards (inline_msg_id);

CREATE INDEX IF NOT EXISTS idx_inline_boards_chat_msg
    ON inline_boards (chat_id, message_id)
    WHERE chat_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_inline_boards_created_at
    ON inline_boards (created_at);
