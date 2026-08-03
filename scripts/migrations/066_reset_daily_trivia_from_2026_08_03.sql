-- 066_reset_daily_trivia_from_2026_08_03.sql
-- One-time cutover to the revisioned Daily Trivia authoring flow.
--
-- Product decision: preserve every result and puzzle before 2026-08-03,
-- while allowing today's and future generated content/results to restart.

DO $$
DECLARE
    cutoff_date CONSTANT DATE := DATE '2026-08-03';
BEGIN
    -- This table did not originally reference puzzles, so remove its rows
    -- explicitly before deleting the current/future puzzle projections.
    DELETE FROM public.daily_trivia_super_results
    WHERE puzzle_date >= cutoff_date;

    DELETE FROM public.daily_trivia_results
    WHERE puzzle_date >= cutoff_date;

    -- Revisions and occurrences are removed through their cascade. Historical
    -- puzzles and both result tables remain byte-for-byte untouched.
    DELETE FROM public.daily_trivia_puzzles
    WHERE puzzle_date >= cutoff_date;

    -- The old transient key table is no longer authoritative, but clearing the
    -- cutover range prevents misleading entries in old tooling.
    DELETE FROM public.daily_trivia_used_keys
    WHERE used_at::DATE >= cutoff_date;

    DELETE FROM public.daily_trivia_question_variants v
    WHERE NOT EXISTS (
        SELECT 1
        FROM public.daily_trivia_question_occurrences o
        WHERE o.variant_id = v.variant_id
    );

    DELETE FROM public.daily_trivia_facts f
    WHERE NOT EXISTS (
        SELECT 1
        FROM public.daily_trivia_question_variants v
        WHERE v.fact_id = f.fact_id
    );
END $$;
