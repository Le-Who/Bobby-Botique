
CREATE INDEX IF NOT EXISTS idx_conv_messages_conv_id
    ON conversation_messages (conversation_id);

CREATE INDEX IF NOT EXISTS idx_memory_edges_target_node
    ON memory_edges (user_id, target_node);
