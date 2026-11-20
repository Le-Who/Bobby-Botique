
ALTER TABLE user_metrics
    ADD COLUMN IF NOT EXISTS current_streak INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS longest_streak INT DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_user_metrics_date
    ON user_metrics (metric_date);

CREATE INDEX IF NOT EXISTS idx_user_metrics_date_user
    ON user_metrics (metric_date, user_id)
    WHERE request_count > 0;
