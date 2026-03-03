-- Add streak tracking columns + analytics indexes.
-- Adds current_streak and longest_streak to user_metrics
-- for habit-loop engagement tracking.

ALTER TABLE user_metrics
    ADD COLUMN IF NOT EXISTS current_streak INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS longest_streak INT DEFAULT 0;

-- Index for retention/DAU analytics queries
CREATE INDEX IF NOT EXISTS idx_user_metrics_date
    ON user_metrics (metric_date);

-- Composite index for "active on date" queries (replaces broken CURRENT_DATE partial index)
CREATE INDEX IF NOT EXISTS idx_user_metrics_date_user
    ON user_metrics (metric_date, user_id)
    WHERE request_count > 0;
