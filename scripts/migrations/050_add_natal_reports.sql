CREATE TABLE IF NOT EXISTS natal_reports (
    report_id TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    chart_json JSONB NOT NULL,
    svg TEXT NOT NULL,
    sections_json JSONB NOT NULL,
    hosted_url TEXT,
    telegraph_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_natal_reports_user_created
    ON natal_reports(user_id, created_at DESC)
    WHERE deleted_at IS NULL;
