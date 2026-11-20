
DO $$
DECLARE
    cutoff_date CONSTANT DATE := DATE '2026-08-03';
BEGIN
    DELETE FROM public.daily_trivia_super_results
    WHERE puzzle_date >= cutoff_date;

    DELETE FROM public.daily_trivia_results
    WHERE puzzle_date >= cutoff_date;

    DELETE FROM public.daily_trivia_puzzles
    WHERE puzzle_date >= cutoff_date;

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
