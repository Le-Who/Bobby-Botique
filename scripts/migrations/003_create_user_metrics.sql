-- Per-user daily metrics table
CREATE TABLE IF NOT EXISTS user_metrics (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    metric_date DATE NOT NULL DEFAULT CURRENT_DATE,
    request_count INT DEFAULT 0,
    model_usage JSONB DEFAULT '{}',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, metric_date)
);

-- Index for fast user lookups
CREATE INDEX IF NOT EXISTS idx_user_metrics_user_date ON user_metrics (user_id, metric_date);
