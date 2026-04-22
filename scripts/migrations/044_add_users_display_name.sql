-- Migration 044: Add display_name column to public.users
-- This column stores the user's Telegram display name (first_name [+ last_name])
-- populated lazily when the user opens the Daily Crocodile Mini App.
-- The column is intentionally nullable so existing rows are unaffected.

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS display_name TEXT;
