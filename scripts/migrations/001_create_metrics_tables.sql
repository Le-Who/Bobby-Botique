-- Migration: Create metrics and error_logs tables
-- Description: Moves runtime DDL from metrics.py to a permanent migration script

-- Таблица для общих метрик
CREATE TABLE IF NOT EXISTS metrics (
    id SERIAL PRIMARY KEY,
    metric_date DATE NOT NULL,
    request_count INTEGER DEFAULT 0,
    total_response_time REAL DEFAULT 0.0,
    error_count INTEGER DEFAULT 0,
    search_queries INTEGER DEFAULT 0,
    cache_hits INTEGER DEFAULT 0,
    cache_misses INTEGER DEFAULT 0,
    api_calls JSONB DEFAULT '{}',
    model_usage JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(metric_date)
);

-- Таблица для ошибок
CREATE TABLE IF NOT EXISTS error_logs (
    id SERIAL PRIMARY KEY,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    request_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Добавление request_id если таблица уже существует
ALTER TABLE error_logs
ADD COLUMN IF NOT EXISTS request_id TEXT;
