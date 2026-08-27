-- Scoped multi-Mac graph feeders: persist graphify source ownership on nodes.
--
-- source_id is set on upsert via source_id_for_graphify_node; delete sync only
-- removes graphify-owned rows whose source_id is in this Mac's enabled and
-- reachable graph_sources ids.

ALTER TABLE nodes ADD COLUMN IF NOT EXISTS source_id TEXT;

CREATE INDEX IF NOT EXISTS idx_nodes_source_id ON nodes (source_id);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0007_nodes_source_id',
    'nodes.source_id for scoped multi-Mac graph feeder delete'
)
ON CONFLICT (version) DO NOTHING;
