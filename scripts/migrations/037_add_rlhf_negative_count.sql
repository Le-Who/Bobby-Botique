-- Migration 037: Add rlhf_negative_count to long_term_memory for provenance-aware RLHF
-- When a user taps 👎, we penalize both the graph edges AND the source memories that
-- generated those edges. This column tracks negative feedback on source memories.

ALTER TABLE long_term_memory
    ADD COLUMN IF NOT EXISTS rlhf_negative_count INT DEFAULT 0;

-- Index for efficient search-time deprioritization of negatively-rated memories
CREATE INDEX IF NOT EXISTS idx_ltm_rlhf_negative
    ON long_term_memory (user_id, rlhf_negative_count)
    WHERE rlhf_negative_count > 0;
