-- 056_bolt_performance_indexes.sql
-- Bolt: add missing indexes for two high-traffic query patterns.
-- Idempotency: all statements use IF NOT EXISTS -- safe to re-run.

-- 1. conversation_messages.conversation_id
-- The table has a FK to conversations(id) ON DELETE CASCADE but no index on that column.
-- Every history fetch (SELECT ... WHERE conversation_id = $1) triggers a Seq Scan.
-- Expected impact: reduces history-fetch latency from O(total_rows) to O(log n + k).
CREATE INDEX IF NOT EXISTS idx_conv_messages_conv_id
    ON conversation_messages (conversation_id);

-- 2. memory_edges.target_node (per user)
-- Graph traversal requires both forward (source_node) and reverse (target_node) lookups.
-- The existing unique index covers (user_id, source_node, target_node, predicate), which
-- is efficient only for forward traversal. Reverse lookups (find all edges pointing TO a
-- node) still require a full per-user scan.
-- Expected impact: eliminates Seq Scan on reverse graph traversal queries.
CREATE INDEX IF NOT EXISTS idx_memory_edges_target_node
    ON memory_edges (user_id, target_node);
