-- 025_add_temporal_graph_edges.sql
-- Add updated_at column to memory_edges for Time-decay traversal
ALTER TABLE memory_edges
ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;

-- Deduplicate existing memory_edges before adding unique index
WITH duplicates AS (
    SELECT id, 
           ROW_NUMBER() OVER(
               PARTITION BY user_id, source_node, target_node, predicate 
               ORDER BY weight DESC, id DESC
           ) as rn
    FROM memory_edges
)
DELETE FROM memory_edges
WHERE id IN (SELECT id FROM duplicates WHERE rn > 1);

-- Create unique index to allow ON CONFLICT Upserts
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_edges_unique 
ON memory_edges(user_id, source_node, target_node, predicate);
