-- Owned transport attempt counts are deliberately separate from legacy logical
-- API maps. Activate the epoch at application initialization, not migration time.
CREATE TABLE IF NOT EXISTS traffic_accounting_epoch (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    started_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS traffic_daily (
    metric_date DATE NOT NULL,
    source_id UUID NOT NULL,
    api_attempts BIGINT NOT NULL DEFAULT 0 CHECK (api_attempts >= 0),
    PRIMARY KEY (metric_date, source_id)
);
