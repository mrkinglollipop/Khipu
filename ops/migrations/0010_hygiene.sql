-- Khipu memory reliability W5 — topic/graph hygiene + forgetting.
--
-- W5.1 topics vs tags: a capture topic must resolve to an existing topic slug
--   (exact match, or via this alias table) before it is minted as a topic:
--   graph node; everything else becomes a tag instead of graph noise
--   (94% of capture-topic slugs dangle today — root cause E).
-- W5.2 path minting filter reads episodes.repo_root (0008); no new column.
-- W5.6 forgetting: episodes.deleted_at soft-deletes an episode out of search
--   and embeddings, matching topics.deleted_at (0003_reconcile_upsert.sql).

CREATE TABLE IF NOT EXISTS topic_aliases (
    alias   TEXT PRIMARY KEY,
    slug    TEXT NOT NULL REFERENCES topics (slug) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_topic_aliases_slug ON topic_aliases (slug);

ALTER TABLE episodes ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_episodes_deleted ON episodes (deleted_at) WHERE deleted_at IS NOT NULL;

COMMENT ON COLUMN episodes.tags IS
    'Capture topic slugs that do NOT resolve to an existing topic page (exact or via topic_aliases) — never minted as topic: graph nodes. See khipu.hygiene.classify_topics.';
COMMENT ON COLUMN episodes.deleted_at IS
    'Soft-delete (khipu episode forget): row excluded from search and its memory_embeddings rows removed. Never hard-deleted.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0010_hygiene',
    'memory reliability W5: topic_aliases table, episodes.tags, episodes.deleted_at'
)
ON CONFLICT (version) DO NOTHING;
