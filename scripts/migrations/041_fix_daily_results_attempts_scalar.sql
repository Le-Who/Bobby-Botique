
UPDATE public.crocodile_daily_results
SET    attempts = '[]'::jsonb
WHERE  jsonb_typeof(attempts) <> 'array';
