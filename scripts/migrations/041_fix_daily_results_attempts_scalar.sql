-- 041_fix_daily_results_attempts_scalar.sql
-- Fix corrupt rows where `attempts` column is a JSONB scalar (null, string, etc.)
-- instead of the expected JSONB array.  This causes
-- `jsonb_array_length(attempts)` in leaderboard/rank queries to crash with
-- "cannot get array length of a scalar".

UPDATE public.crocodile_daily_results
SET    attempts = '[]'::jsonb
WHERE  jsonb_typeof(attempts) <> 'array';
