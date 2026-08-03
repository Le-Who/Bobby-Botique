-- 065_daily_trivia_question_bank.sql
-- Additive Daily Trivia authoring/question-bank model.
-- Existing puzzle and result rows are preserved; legacy JSON remains as a
-- compatibility projection while callers move to revisions and occurrences.

CREATE TABLE IF NOT EXISTS public.daily_trivia_facts (
    fact_id BIGSERIAL PRIMARY KEY,
    subject_norm TEXT NOT NULL,
    relation_norm TEXT NOT NULL,
    answer_norm TEXT NOT NULL,
    identity_hash TEXT NOT NULL UNIQUE,
    canonical_claim TEXT NOT NULL,
    embedding halfvec(768),
    identity_version INTEGER NOT NULL DEFAULT 1,
    first_used_at DATE,
    last_used_at DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.daily_trivia_question_variants (
    variant_id BIGSERIAL PRIMARY KEY,
    fact_id BIGINT NOT NULL REFERENCES public.daily_trivia_facts(fact_id) ON DELETE RESTRICT,
    topic TEXT NOT NULL,
    question TEXT NOT NULL,
    options JSONB NOT NULL,
    correct_index INTEGER NOT NULL,
    explanation TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'llm',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT daily_trivia_variant_difficulty_check CHECK (difficulty IN ('main', 'super')),
    CONSTRAINT daily_trivia_variant_correct_index_check CHECK (correct_index BETWEEN 0 AND 3),
    CONSTRAINT daily_trivia_variant_options_check CHECK (jsonb_array_length(options) = 4)
);

CREATE TABLE IF NOT EXISTS public.daily_trivia_puzzle_revisions (
    revision_id BIGSERIAL PRIMARY KEY,
    puzzle_date DATE NOT NULL REFERENCES public.daily_trivia_puzzles(puzzle_date) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    actor TEXT NOT NULL DEFAULT 'scheduler',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    CONSTRAINT daily_trivia_revision_status_check CHECK (status IN ('draft', 'review_required', 'ready', 'disabled')),
    CONSTRAINT daily_trivia_revision_unique UNIQUE (puzzle_date, revision_no)
);

CREATE TABLE IF NOT EXISTS public.daily_trivia_question_occurrences (
    revision_id BIGINT NOT NULL REFERENCES public.daily_trivia_puzzle_revisions(revision_id) ON DELETE CASCADE,
    lane TEXT NOT NULL,
    position INTEGER NOT NULL,
    variant_id BIGINT NOT NULL REFERENCES public.daily_trivia_question_variants(variant_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (revision_id, lane, position),
    CONSTRAINT daily_trivia_occurrence_lane_check CHECK (lane IN ('main', 'super')),
    CONSTRAINT daily_trivia_occurrence_position_check CHECK (
        (lane = 'main' AND position BETWEEN 1 AND 5)
        OR (lane = 'super' AND position BETWEEN 1 AND 3)
    )
);

ALTER TABLE public.daily_trivia_puzzles
    ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS published_revision_id BIGINT;

ALTER TABLE public.daily_trivia_results
    ADD COLUMN IF NOT EXISTS puzzle_revision_id BIGINT;

ALTER TABLE public.daily_trivia_super_results
    ADD COLUMN IF NOT EXISTS puzzle_revision_id BIGINT;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'daily_trivia_published_revision_fk'
          AND conrelid = 'public.daily_trivia_puzzles'::regclass
    ) THEN
        ALTER TABLE public.daily_trivia_puzzles
            ADD CONSTRAINT daily_trivia_published_revision_fk
            FOREIGN KEY (published_revision_id)
            REFERENCES public.daily_trivia_puzzle_revisions(revision_id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'daily_trivia_result_revision_fk'
          AND conrelid = 'public.daily_trivia_results'::regclass
    ) THEN
        ALTER TABLE public.daily_trivia_results
            ADD CONSTRAINT daily_trivia_result_revision_fk
            FOREIGN KEY (puzzle_revision_id)
            REFERENCES public.daily_trivia_puzzle_revisions(revision_id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'daily_trivia_super_result_revision_fk'
          AND conrelid = 'public.daily_trivia_super_results'::regclass
    ) THEN
        ALTER TABLE public.daily_trivia_super_results
            ADD CONSTRAINT daily_trivia_super_result_revision_fk
            FOREIGN KEY (puzzle_revision_id)
            REFERENCES public.daily_trivia_puzzle_revisions(revision_id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS daily_trivia_facts_usage_idx
    ON public.daily_trivia_facts (last_used_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS daily_trivia_variants_fact_idx
    ON public.daily_trivia_question_variants (fact_id);

CREATE INDEX IF NOT EXISTS daily_trivia_occurrences_variant_idx
    ON public.daily_trivia_question_occurrences (variant_id);

CREATE INDEX IF NOT EXISTS daily_trivia_revisions_date_status_idx
    ON public.daily_trivia_puzzle_revisions (puzzle_date DESC, status, revision_no DESC);

CREATE INDEX IF NOT EXISTS daily_trivia_revisions_ready_date_idx
    ON public.daily_trivia_puzzle_revisions (puzzle_date DESC, revision_id)
    WHERE status = 'ready';

CREATE INDEX IF NOT EXISTS daily_trivia_puzzles_published_revision_idx
    ON public.daily_trivia_puzzles (published_revision_id)
    WHERE published_revision_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS daily_trivia_results_revision_idx
    ON public.daily_trivia_results (puzzle_revision_id)
    WHERE puzzle_revision_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS daily_trivia_super_results_revision_idx
    ON public.daily_trivia_super_results (puzzle_revision_id)
    WHERE puzzle_revision_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS daily_trivia_facts_embedding_idx
    ON public.daily_trivia_facts USING hnsw (embedding halfvec_cosine_ops)
    WHERE embedding IS NOT NULL;

-- Idempotent legacy backfill. Historical result rows are deliberately not
-- updated; NULL puzzle_revision_id means "resolve the immutable legacy day".
--
-- Older asyncpg write paths double-encoded these JSONB arrays, leaving a JSON
-- string that contains the serialized array.  Unwrap that representation
-- before expanding it, then guard every expansion so NULL/object/scalar rows
-- are skipped instead of aborting the whole transactional migration.
UPDATE public.daily_trivia_puzzles
SET questions = (questions #>> '{}')::jsonb
WHERE jsonb_typeof(questions) = 'string';

UPDATE public.daily_trivia_puzzles
SET super_questions = (super_questions #>> '{}')::jsonb
WHERE jsonb_typeof(super_questions) = 'string';

INSERT INTO public.daily_trivia_puzzle_revisions (
    puzzle_date, revision_no, status, actor, created_at, published_at
)
SELECT p.puzzle_date,
       1,
       CASE WHEN p.status = 'ready' THEN 'ready' ELSE p.status END,
       'legacy_import',
       p.created_at,
       p.prepared_at
FROM public.daily_trivia_puzzles p
ON CONFLICT (puzzle_date, revision_no) DO NOTHING;

WITH legacy_questions AS (
    SELECT p.puzzle_date,
           lane,
           q.value AS payload,
           q.ordinality::INTEGER AS position,
           LOWER(REGEXP_REPLACE(COALESCE(q.value->>'question', ''), E'\\s+', ' ', 'g')) AS question_norm,
           LOWER(COALESCE(
               q.value#>>'{identity,answer}',
               (q.value->'options')->>COALESCE((q.value->>'correct_index')::INTEGER, 0),
               ''
           )) AS answer_norm
    FROM public.daily_trivia_puzzles p
    CROSS JOIN LATERAL (
        SELECT 'main'::TEXT AS lane, value, ordinality
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(p.questions) = 'array' THEN p.questions ELSE '[]'::jsonb END
        ) WITH ORDINALITY
        UNION ALL
        SELECT 'super'::TEXT AS lane, value, ordinality
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(p.super_questions) = 'array' THEN p.super_questions ELSE '[]'::jsonb END
        ) WITH ORDINALITY
    ) q
), identities AS (
    SELECT *, MD5(question_norm || CHR(31) || answer_norm) AS legacy_identity_hash
    FROM legacy_questions
    WHERE question_norm <> ''
)
INSERT INTO public.daily_trivia_facts (
    subject_norm, relation_norm, answer_norm, identity_hash, canonical_claim,
    identity_version, first_used_at, last_used_at
)
SELECT MIN(question_norm),
       'legacy_question',
       MIN(answer_norm),
       legacy_identity_hash,
       MIN(question_norm || ' — ответ: ' || answer_norm),
       0,
       MIN(puzzle_date),
       MAX(puzzle_date)
FROM identities
GROUP BY legacy_identity_hash
ON CONFLICT (identity_hash) DO UPDATE
SET first_used_at = LEAST(public.daily_trivia_facts.first_used_at, EXCLUDED.first_used_at),
    last_used_at = GREATEST(public.daily_trivia_facts.last_used_at, EXCLUDED.last_used_at),
    updated_at = NOW();

WITH legacy_questions AS (
    SELECT p.puzzle_date,
           lane,
           q.value AS payload,
           q.ordinality::INTEGER AS position,
           LOWER(REGEXP_REPLACE(COALESCE(q.value->>'question', ''), E'\\s+', ' ', 'g')) AS question_norm,
           LOWER(COALESCE(
               q.value#>>'{identity,answer}',
               (q.value->'options')->>COALESCE((q.value->>'correct_index')::INTEGER, 0),
               ''
           )) AS answer_norm
    FROM public.daily_trivia_puzzles p
    CROSS JOIN LATERAL (
        SELECT 'main'::TEXT AS lane, value, ordinality
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(p.questions) = 'array' THEN p.questions ELSE '[]'::jsonb END
        ) WITH ORDINALITY
        UNION ALL
        SELECT 'super'::TEXT AS lane, value, ordinality
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(p.super_questions) = 'array' THEN p.super_questions ELSE '[]'::jsonb END
        ) WITH ORDINALITY
    ) q
), prepared AS (
    SELECT *,
           MD5(question_norm || CHR(31) || answer_norm) AS legacy_identity_hash,
           MD5(lane || CHR(31) || COALESCE(payload->>'question', '') || CHR(31) || COALESCE(payload->'options', '[]'::jsonb)::TEXT) AS content_hash
    FROM legacy_questions
    WHERE question_norm <> ''
      AND jsonb_typeof(payload->'options') = 'array'
      AND jsonb_array_length(payload->'options') = 4
      AND COALESCE(payload->>'correct_index', '0') ~ '^[0-3]$'
)
INSERT INTO public.daily_trivia_question_variants (
    fact_id, topic, question, options, correct_index, explanation,
    difficulty, content_hash, source
)
SELECT f.fact_id,
       COALESCE(p.payload->>'topic', 'Общие знания'),
       COALESCE(p.payload->>'question', ''),
       COALESCE(p.payload->'options', '[]'::jsonb),
       COALESCE((p.payload->>'correct_index')::INTEGER, 0),
       COALESCE(p.payload->>'explanation', ''),
       p.lane,
       p.content_hash,
       'legacy_import'
FROM prepared p
JOIN public.daily_trivia_facts f ON f.identity_hash = p.legacy_identity_hash
ON CONFLICT (content_hash) DO NOTHING;

WITH legacy_questions AS (
    SELECT p.puzzle_date,
           lane,
           q.value AS payload,
           q.ordinality::INTEGER AS position,
           MD5(lane || CHR(31) || COALESCE(q.value->>'question', '') || CHR(31) || COALESCE(q.value->'options', '[]'::jsonb)::TEXT) AS content_hash
    FROM public.daily_trivia_puzzles p
    CROSS JOIN LATERAL (
        SELECT 'main'::TEXT AS lane, value, ordinality
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(p.questions) = 'array' THEN p.questions ELSE '[]'::jsonb END
        ) WITH ORDINALITY
        UNION ALL
        SELECT 'super'::TEXT AS lane, value, ordinality
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(p.super_questions) = 'array' THEN p.super_questions ELSE '[]'::jsonb END
        ) WITH ORDINALITY
    ) q
)
INSERT INTO public.daily_trivia_question_occurrences (revision_id, lane, position, variant_id)
SELECT r.revision_id, q.lane, q.position, v.variant_id
FROM legacy_questions q
JOIN public.daily_trivia_puzzle_revisions r
  ON r.puzzle_date = q.puzzle_date AND r.revision_no = 1
JOIN public.daily_trivia_question_variants v ON v.content_hash = q.content_hash
ON CONFLICT (revision_id, lane, position) DO NOTHING;

UPDATE public.daily_trivia_puzzles p
SET revision = GREATEST(p.revision, 1),
    published_revision_id = COALESCE(p.published_revision_id, r.revision_id)
FROM public.daily_trivia_puzzle_revisions r
WHERE r.puzzle_date = p.puzzle_date
  AND r.revision_no = 1
  AND (p.revision = 0 OR p.published_revision_id IS NULL);
