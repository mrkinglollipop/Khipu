-- Khipu memory reliability — indexes for lineage lookups (fix 15).
--
-- capture._inherit_project and activity.project_slice both filter episodes
-- by parent_session_id (a dispatched child inheriting project from its
-- lineage) and by session_id (the exact-window dedup, the probe's own
-- session lookup); neither had a dedicated index — migration 0008 indexed
-- (project) and (project, ts) but not these two lookup columns.

CREATE INDEX IF NOT EXISTS idx_episodes_parent_session ON episodes (parent_session_id);
CREATE INDEX IF NOT EXISTS idx_episodes_session_id ON episodes (session_id);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0011_identity_indexes',
    'memory reliability: idx_episodes_parent_session + idx_episodes_session_id'
)
ON CONFLICT (version) DO NOTHING;
