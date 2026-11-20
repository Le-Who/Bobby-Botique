
ALTER TABLE long_term_memory
    ADD COLUMN IF NOT EXISTS rlhf_negative_count INT DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_ltm_rlhf_negative
    ON long_term_memory (user_id, rlhf_negative_count)
    WHERE rlhf_negative_count > 0;
