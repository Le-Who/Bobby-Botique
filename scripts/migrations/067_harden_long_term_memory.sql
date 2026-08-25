-- Migration 067: durable LTM consent, normalized GraphRAG provenance, and retention safety.
-- This migration is additive/idempotent and intentionally does not FORCE RLS so the
-- database owner and configured service roles retain their documented PostgreSQL behavior.

ALTER TABLE chats
    ADD COLUMN IF NOT EXISTS memory_epoch BIGINT NOT NULL DEFAULT 0;

ALTER TABLE long_term_memory
    ADD COLUMN IF NOT EXISTS consolidated_at TIMESTAMPTZ;

ALTER TABLE memory_nodes
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- A queued capture carrying the old epoch must become stale as soon as the user
-- changes consent from enabled to disabled, regardless of which app process writes it.
CREATE OR REPLACE FUNCTION bump_memory_epoch_on_ltm_disable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.ltm_enabled IS FALSE THEN
            NEW.memory_epoch := GREATEST(NEW.memory_epoch, 1);
        END IF;
    ELSIF OLD.ltm_enabled IS TRUE AND NEW.ltm_enabled IS FALSE THEN
        NEW.memory_epoch := OLD.memory_epoch + 1;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS chats_bump_memory_epoch_on_disable ON chats;
CREATE TRIGGER chats_bump_memory_epoch_on_disable
    BEFORE INSERT OR UPDATE OF ltm_enabled ON chats
    FOR EACH ROW
    EXECUTE FUNCTION bump_memory_epoch_on_ltm_disable();

-- Composite uniqueness lets provenance enforce that its edge, memory, and tenant
-- all agree. The primary keys still remain the canonical single-column BIGINT IDs.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_edges_id_user_id
    ON memory_edges (id, user_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ltm_id_user_id
    ON long_term_memory (id, user_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_nodes_id_user_id
    ON memory_nodes (id, user_id);

-- Add tenant endpoint constraints as NOT VALID first: new/updated rows are protected
-- immediately while legacy corruption can be removed without an initial table scan.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'memory_edges_source_tenant_fk'
          AND conrelid = 'memory_edges'::regclass
    ) THEN
        ALTER TABLE memory_edges
            ADD CONSTRAINT memory_edges_source_tenant_fk
            FOREIGN KEY (source_node, user_id)
            REFERENCES memory_nodes (id, user_id) ON DELETE CASCADE
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'memory_edges_target_tenant_fk'
          AND conrelid = 'memory_edges'::regclass
    ) THEN
        ALTER TABLE memory_edges
            ADD CONSTRAINT memory_edges_target_tenant_fk
            FOREIGN KEY (target_node, user_id)
            REFERENCES memory_nodes (id, user_id) ON DELETE CASCADE
            NOT VALID;
    END IF;
END;
$$;

-- Cross-tenant endpoint rows cannot be made authoritative. Remove them before
-- validating; valid historical edges and their source arrays remain untouched.
DELETE FROM memory_edges AS edge
WHERE NOT EXISTS (
        SELECT 1
        FROM memory_nodes AS source
        WHERE source.id = edge.source_node
          AND source.user_id = edge.user_id
    )
   OR NOT EXISTS (
        SELECT 1
        FROM memory_nodes AS target
        WHERE target.id = edge.target_node
          AND target.user_id = edge.user_id
    );

ALTER TABLE memory_edges
    VALIDATE CONSTRAINT memory_edges_source_tenant_fk;
ALTER TABLE memory_edges
    VALIDATE CONSTRAINT memory_edges_target_tenant_fk;

CREATE TABLE IF NOT EXISTS memory_edge_sources (
    edge_id BIGINT NOT NULL,
    memory_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    predicate TEXT,
    predicate_embedding halfvec(768),
    weight DOUBLE PRECISION,
    is_core BOOLEAN,
    attributes_complete BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (edge_id, memory_id),
    CONSTRAINT memory_edge_sources_edge_tenant_fk
        FOREIGN KEY (edge_id, user_id)
        REFERENCES memory_edges (id, user_id) ON DELETE CASCADE,
    CONSTRAINT memory_edge_sources_memory_tenant_fk
        FOREIGN KEY (memory_id, user_id)
        REFERENCES long_term_memory (id, user_id) ON DELETE CASCADE
);

-- Re-runs must also upgrade a table created by an earlier revision of this
-- migration. Legacy rows are intentionally incomplete: one canonical edge row
-- cannot prove which source supplied weight/core/predicate.
ALTER TABLE memory_edge_sources
    ADD COLUMN IF NOT EXISTS predicate TEXT,
    ADD COLUMN IF NOT EXISTS predicate_embedding halfvec(768),
    ADD COLUMN IF NOT EXISTS weight DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS is_core BOOLEAN,
    ADD COLUMN IF NOT EXISTS attributes_complete BOOLEAN NOT NULL DEFAULT FALSE;

-- Mutable node values need their own exact source snapshots. The canonical node
-- remains a retrieval projection and is rebuilt from these rows after deletion.
CREATE TABLE IF NOT EXISTS memory_node_sources (
    node_id BIGINT NOT NULL,
    memory_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    entity_type TEXT NOT NULL,
    description TEXT,
    embedding halfvec(768),
    wing TEXT,
    room TEXT,
    file_id TEXT,
    file_type TEXT,
    attributes_complete BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, memory_id),
    CONSTRAINT memory_node_sources_node_tenant_fk
        FOREIGN KEY (node_id, user_id)
        REFERENCES memory_nodes (id, user_id) ON DELETE CASCADE,
    CONSTRAINT memory_node_sources_memory_tenant_fk
        FOREIGN KEY (memory_id, user_id)
        REFERENCES long_term_memory (id, user_id) ON DELETE CASCADE
);

-- Exact raw-memory support for derived/consolidated facts. Deleting any cited raw
-- source invalidates the derived fact conservatively via the trigger below.
CREATE TABLE IF NOT EXISTS memory_derivation_sources (
    derived_memory_id BIGINT NOT NULL,
    source_memory_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (derived_memory_id, source_memory_id),
    CONSTRAINT memory_derivation_sources_distinct_check
        CHECK (derived_memory_id <> source_memory_id),
    CONSTRAINT memory_derivation_sources_derived_tenant_fk
        FOREIGN KEY (derived_memory_id, user_id)
        REFERENCES long_term_memory (id, user_id) ON DELETE CASCADE,
    CONSTRAINT memory_derivation_sources_source_tenant_fk
        FOREIGN KEY (source_memory_id, user_id)
        REFERENCES long_term_memory (id, user_id) ON DELETE CASCADE
);

CREATE OR REPLACE FUNCTION delete_derived_memory_on_source_removal()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(OLD.user_id);
    DELETE FROM long_term_memory
    WHERE id = OLD.derived_memory_id
      AND user_id = OLD.user_id;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS memory_derivation_sources_delete_derived ON memory_derivation_sources;
CREATE TRIGGER memory_derivation_sources_delete_derived
    AFTER DELETE ON memory_derivation_sources
    FOR EACH ROW
    EXECUTE FUNCTION delete_derived_memory_on_source_removal();

-- Backfill only valid, same-tenant legacy references. Dangling or cross-tenant array
-- entries are deliberately ignored instead of being made authoritative.
INSERT INTO memory_edge_sources (
    edge_id, memory_id, user_id, predicate, predicate_embedding,
    weight, is_core, attributes_complete
)
SELECT DISTINCT e.id, legacy.memory_id, e.user_id, e.predicate,
       e.predicate_embedding, COALESCE(e.weight, 1.0),
       COALESCE(e.is_core, FALSE), FALSE
FROM memory_edges AS e
CROSS JOIN LATERAL unnest(COALESCE(e.source_memory_ids, '{}'::BIGINT[])) AS legacy(memory_id)
JOIN long_term_memory AS memory
  ON memory.id = legacy.memory_id
 AND memory.user_id = e.user_id
ON CONFLICT (edge_id, memory_id) DO NOTHING;

-- Upgrade rows created by an earlier 067 revision without pretending their
-- canonical attributes have exact provenance.
UPDATE memory_edge_sources AS source
SET predicate = COALESCE(source.predicate, edge.predicate),
    predicate_embedding = COALESCE(source.predicate_embedding, edge.predicate_embedding),
    weight = COALESCE(source.weight, edge.weight, 1.0),
    is_core = COALESCE(source.is_core, edge.is_core, FALSE)
FROM memory_edges AS edge
WHERE edge.id = source.edge_id
  AND edge.user_id = source.user_id
  AND (
      source.predicate IS NULL
      OR source.weight IS NULL
      OR source.is_core IS NULL
  );

ALTER TABLE memory_edge_sources
    ALTER COLUMN predicate SET NOT NULL,
    ALTER COLUMN weight SET NOT NULL,
    ALTER COLUMN is_core SET NOT NULL;

-- A legacy node can be linked to several memories through its incident edges,
-- but there is no evidence identifying which one contributed its mutable values.
-- Backfill conservatively and mark those snapshots incomplete. On source removal
-- the trigger below clears rather than re-attributing those values.
INSERT INTO memory_node_sources (
    node_id, memory_id, user_id, entity_type, description, embedding,
    wing, room, file_id, file_type, attributes_complete
)
SELECT DISTINCT node.id, source.memory_id, node.user_id,
       node.entity_type, node.description, node.embedding,
       node.wing, node.room, node.file_id, node.file_type, FALSE
FROM memory_nodes AS node
JOIN memory_edges AS edge
  ON edge.user_id = node.user_id
 AND (edge.source_node = node.id OR edge.target_node = node.id)
JOIN memory_edge_sources AS source
  ON source.edge_id = edge.id
 AND source.user_id = edge.user_id
ON CONFLICT (node_id, memory_id) DO NOTHING;

-- If a deployment accumulated duplicate current facts without migration 025's full
-- unique index, preserve every source on the strongest row before removing duplicates.
WITH ranked_current_edges AS (
    SELECT
        id,
        FIRST_VALUE(id) OVER (
            PARTITION BY user_id, source_node, target_node, predicate
            ORDER BY weight DESC NULLS LAST, updated_at DESC NULLS LAST, id DESC
        ) AS winner_id,
        ROW_NUMBER() OVER (
            PARTITION BY user_id, source_node, target_node, predicate
            ORDER BY weight DESC NULLS LAST, updated_at DESC NULLS LAST, id DESC
        ) AS row_number
    FROM memory_edges
    WHERE valid_to IS NULL
), moved_sources AS (
    INSERT INTO memory_edge_sources (
        edge_id, memory_id, user_id, predicate, predicate_embedding,
        weight, is_core, attributes_complete
    )
    SELECT ranked.winner_id, sources.memory_id, sources.user_id,
           sources.predicate, sources.predicate_embedding,
           sources.weight, sources.is_core, sources.attributes_complete
    FROM ranked_current_edges AS ranked
    JOIN memory_edge_sources AS sources ON sources.edge_id = ranked.id
    WHERE ranked.row_number > 1
    ON CONFLICT (edge_id, memory_id) DO NOTHING
    RETURNING edge_id
)
DELETE FROM memory_edges AS edge
USING ranked_current_edges AS ranked
WHERE edge.id = ranked.id
  AND ranked.row_number > 1;

-- Historical versions may repeat a triple. Only the current version is unique.
DROP INDEX IF EXISTS idx_memory_edges_unique;
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_edges_current_unique
    ON memory_edges (user_id, source_node, target_node, predicate)
    WHERE valid_to IS NULL;

-- Return TRUE when the edge had to be removed. Complete source snapshots can
-- deterministically rebuild every mutable edge attribute. For legacy/incomplete
-- snapshots, deleting any source removes the edge rather than retaining a value
-- that may have come exclusively from the deleted memory.
CREATE OR REPLACE FUNCTION recompute_memory_edge_after_source_removal(
    target_edge_id BIGINT,
    target_user_id BIGINT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    source_weight DOUBLE PRECISION;
    source_is_core BOOLEAN;
    source_predicate TEXT;
    source_predicate_embedding halfvec(768);
    live_source_ids BIGINT[];
BEGIN
    PERFORM pg_advisory_xact_lock(target_user_id);
    PERFORM 1
    FROM memory_edges
    WHERE id = target_edge_id
      AND user_id = target_user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM memory_edge_sources
        WHERE edge_id = target_edge_id
          AND user_id = target_user_id
    ) OR EXISTS (
        SELECT 1
        FROM memory_edge_sources
        WHERE edge_id = target_edge_id
          AND user_id = target_user_id
          AND attributes_complete IS FALSE
    ) THEN
        DELETE FROM memory_edges
        WHERE id = target_edge_id
          AND user_id = target_user_id;
        RETURN FOUND;
    END IF;

    SELECT MAX(weight), BOOL_OR(is_core), ARRAY_AGG(memory_id ORDER BY memory_id)
    INTO source_weight, source_is_core, live_source_ids
    FROM memory_edge_sources
    WHERE edge_id = target_edge_id
      AND user_id = target_user_id;

    SELECT predicate, predicate_embedding
    INTO source_predicate, source_predicate_embedding
    FROM memory_edge_sources
    WHERE edge_id = target_edge_id
      AND user_id = target_user_id
    ORDER BY created_at DESC, memory_id DESC
    LIMIT 1;

    UPDATE memory_edges
    SET predicate = source_predicate,
        predicate_embedding = source_predicate_embedding,
        weight = source_weight,
        is_core = source_is_core,
        source_memory_ids = live_source_ids,
        updated_at = now()
    WHERE id = target_edge_id
      AND user_id = target_user_id;
    RETURN FALSE;
END;
$$;

-- Removing a memory cascades its provenance rows. Recompute the surviving edge,
-- or remove it conservatively, then delete endpoint nodes only when no edge uses them.
CREATE OR REPLACE FUNCTION delete_orphaned_memory_edge()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    orphan_source_node BIGINT;
    orphan_target_node BIGINT;
    edge_deleted BOOLEAN;
BEGIN
    PERFORM pg_advisory_xact_lock(OLD.user_id);

    SELECT edge.source_node, edge.target_node
    INTO orphan_source_node, orphan_target_node
    FROM memory_edges AS edge
    WHERE edge.id = OLD.edge_id
      AND edge.user_id = OLD.user_id
    FOR UPDATE;

    IF NOT EXISTS (
        SELECT 1
        FROM memory_edges
        WHERE id = OLD.edge_id
          AND user_id = OLD.user_id
    ) THEN
        RETURN NULL;
    END IF;

    PERFORM node.id
    FROM memory_nodes AS node
    WHERE node.user_id = OLD.user_id
      AND node.id IN (orphan_source_node, orphan_target_node)
    ORDER BY node.id
    FOR UPDATE;

    edge_deleted := recompute_memory_edge_after_source_removal(OLD.edge_id, OLD.user_id);
    IF edge_deleted THEN
        DELETE FROM memory_nodes AS node
        WHERE node.user_id = OLD.user_id
          AND node.id IN (orphan_source_node, orphan_target_node)
          AND NOT EXISTS (
              SELECT 1
              FROM memory_edges AS edge
              WHERE edge.user_id = OLD.user_id
                AND (edge.source_node = node.id OR edge.target_node = node.id)
          );
    END IF;
    RETURN NULL;
END;
$$;

-- Mutable node values are a projection of exact source snapshots. A legacy
-- incomplete survivor is privacy-safe only after clearing all source-derived
-- values; the stable entity_name remains because live edges still reference it.
CREATE OR REPLACE FUNCTION recompute_memory_node_after_source_removal()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    source_entity_type TEXT;
    source_description TEXT;
    source_embedding halfvec(768);
    source_wing TEXT;
    source_room TEXT;
    source_file_id TEXT;
    source_file_type TEXT;
BEGIN
    PERFORM pg_advisory_xact_lock(OLD.user_id);
    PERFORM 1
    FROM memory_nodes
    WHERE id = OLD.node_id
      AND user_id = OLD.user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM memory_node_sources
        WHERE node_id = OLD.node_id
          AND user_id = OLD.user_id
          AND attributes_complete IS FALSE
    ) THEN
        UPDATE memory_nodes
        SET entity_type = 'concept',
            description = NULL,
            embedding = NULL,
            wing = NULL,
            room = NULL,
            file_id = NULL,
            file_type = NULL,
            updated_at = now()
        WHERE id = OLD.node_id
          AND user_id = OLD.user_id;
        RETURN NULL;
    END IF;

    SELECT entity_type, description, embedding, wing, room
    INTO source_entity_type, source_description, source_embedding, source_wing, source_room
    FROM memory_node_sources
    WHERE node_id = OLD.node_id
      AND user_id = OLD.user_id
    ORDER BY created_at DESC, memory_id DESC
    LIMIT 1;

    IF NOT FOUND THEN
        source_entity_type := 'concept';
        source_description := NULL;
        source_embedding := NULL;
        source_wing := NULL;
        source_room := NULL;
    END IF;

    SELECT file_id, file_type
    INTO source_file_id, source_file_type
    FROM memory_node_sources
    WHERE node_id = OLD.node_id
      AND user_id = OLD.user_id
      AND file_id IS NOT NULL
    ORDER BY created_at DESC, memory_id DESC
    LIMIT 1;

    UPDATE memory_nodes
    SET entity_type = source_entity_type,
        description = source_description,
        embedding = source_embedding,
        wing = source_wing,
        room = source_room,
        file_id = source_file_id,
        file_type = source_file_type,
        updated_at = now()
    WHERE id = OLD.node_id
      AND user_id = OLD.user_id;
    RETURN NULL;
END;
$$;

-- Extraction can fail after inserting a node but before creating any relation.
-- Sweep only old, never-referenced nodes so an in-flight multi-phase extraction gets
-- a generous grace period while eventual retention still removes orphaned PII.
CREATE OR REPLACE FUNCTION delete_stale_orphaned_memory_nodes(target_user_id BIGINT)
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    deleted_edges BIGINT;
    deleted_count BIGINT;
BEGIN
    PERFORM pg_advisory_xact_lock(target_user_id);

    -- Legacy/backfill edges with no normalized live provenance cannot support graph
    -- facts. Remove them before looking for nodes that no edge references.
    WITH unsupported_edges AS (
        SELECT edge.id, edge.user_id
        FROM memory_edges AS edge
        WHERE edge.user_id = target_user_id
          AND COALESCE(edge.updated_at, edge.created_at) < now() - INTERVAL '1 hour'
          AND NOT EXISTS (
              SELECT 1
              FROM memory_edge_sources AS source
              WHERE source.edge_id = edge.id
                AND source.user_id = edge.user_id
          )
        ORDER BY edge.id
        FOR UPDATE OF edge SKIP LOCKED
    )
    DELETE FROM memory_edges AS edge
    USING unsupported_edges AS unsupported
    WHERE edge.id = unsupported.id
      AND edge.user_id = unsupported.user_id;

    GET DIAGNOSTICS deleted_edges = ROW_COUNT;

    WITH orphan_candidates AS (
        SELECT node.id, node.user_id
        FROM memory_nodes AS node
        WHERE node.user_id = target_user_id
          AND node.updated_at < now() - INTERVAL '1 hour'
          AND NOT EXISTS (
              SELECT 1
              FROM memory_edges AS edge
              WHERE edge.user_id = node.user_id
                AND (
                    edge.source_node = node.id
                    OR edge.target_node = node.id
                )
          )
        ORDER BY node.user_id, node.id
        FOR UPDATE OF node SKIP LOCKED
    )
    DELETE FROM memory_nodes AS node
    USING orphan_candidates AS orphan
    WHERE node.id = orphan.id
      AND node.user_id = orphan.user_id;

    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_edges + deleted_count;
END;
$$;

DROP TRIGGER IF EXISTS memory_edge_sources_delete_orphan ON memory_edge_sources;
CREATE CONSTRAINT TRIGGER memory_edge_sources_delete_orphan
    AFTER DELETE ON memory_edge_sources
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION delete_orphaned_memory_edge();

DROP TRIGGER IF EXISTS memory_node_sources_recompute_after_delete ON memory_node_sources;
CREATE CONSTRAINT TRIGGER memory_node_sources_recompute_after_delete
    AFTER DELETE ON memory_node_sources
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION recompute_memory_node_after_source_removal();

-- Tenant, retention, and foreign-key support indexes.
CREATE INDEX IF NOT EXISTS idx_ltm_user_created_at
    ON long_term_memory (user_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_ltm_expires_at
    ON long_term_memory (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ltm_unconsolidated
    ON long_term_memory (user_id, created_at, id)
    WHERE consolidated_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memory_edges_source_node_fk
    ON memory_edges (source_node, user_id);

CREATE INDEX IF NOT EXISTS idx_memory_edges_target_node_fk
    ON memory_edges (target_node, user_id);

CREATE INDEX IF NOT EXISTS idx_memory_edge_sources_memory_user
    ON memory_edge_sources (memory_id, user_id);

CREATE INDEX IF NOT EXISTS idx_memory_edge_sources_user_edge
    ON memory_edge_sources (user_id, edge_id);

CREATE INDEX IF NOT EXISTS idx_memory_node_sources_memory_user
    ON memory_node_sources (memory_id, user_id);

CREATE INDEX IF NOT EXISTS idx_memory_node_sources_user_node
    ON memory_node_sources (user_id, node_id);

CREATE INDEX IF NOT EXISTS idx_memory_derivation_sources_source_user
    ON memory_derivation_sources (source_memory_id, user_id);

CREATE INDEX IF NOT EXISTS idx_memory_derivation_sources_user_derived
    ON memory_derivation_sources (user_id, derived_memory_id);

-- LTM and every derived graph/provenance table share the same tenant boundary.
ALTER TABLE long_term_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_edge_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_node_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_derivation_sources ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS memory_user_isolation ON long_term_memory;
CREATE POLICY memory_user_isolation ON long_term_memory
    FOR ALL
    USING (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    )
    WITH CHECK (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    );

DROP POLICY IF EXISTS memory_nodes_user_policy ON memory_nodes;
CREATE POLICY memory_nodes_user_policy ON memory_nodes
    FOR ALL
    USING (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    )
    WITH CHECK (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    );

DROP POLICY IF EXISTS memory_edges_user_policy ON memory_edges;
CREATE POLICY memory_edges_user_policy ON memory_edges
    FOR ALL
    USING (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    )
    WITH CHECK (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    );

DROP POLICY IF EXISTS memory_edge_sources_user_policy ON memory_edge_sources;
CREATE POLICY memory_edge_sources_user_policy ON memory_edge_sources
    FOR ALL
    USING (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    )
    WITH CHECK (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    );

DROP POLICY IF EXISTS memory_node_sources_user_policy ON memory_node_sources;
CREATE POLICY memory_node_sources_user_policy ON memory_node_sources
    FOR ALL
    USING (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    )
    WITH CHECK (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    );

DROP POLICY IF EXISTS memory_derivation_sources_user_policy ON memory_derivation_sources;
CREATE POLICY memory_derivation_sources_user_policy ON memory_derivation_sources
    FOR ALL
    USING (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    )
    WITH CHECK (
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    );
