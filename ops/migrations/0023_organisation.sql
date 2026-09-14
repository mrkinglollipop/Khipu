-- Khipu organisation without the user (Phase 5, 2026-09-14 "memory that works like magic").
--
-- topics.superseded_by: set by `khipu notes supersede OLD NEW` (R6/G5) and by the
-- slug-collision redirect (G3) so a search hit on a superseded page can point
-- straight at its replacement instead of making the reader guess.
--
-- topic_hits: G6 — the query log records each search's top hit ids but nothing
-- aggregated them per topic, so "what does recall actually use" was
-- unanswerable. Populated by khipu.topic_hits (nightly full recompute +
-- Stop-hook incremental catch-up), read by khipu.organise's index ranking
-- and by khipu notes stale.

ALTER TABLE topics ADD COLUMN IF NOT EXISTS superseded_by TEXT;

CREATE TABLE IF NOT EXISTS topic_hits (
    slug            TEXT PRIMARY KEY REFERENCES topics (slug) ON DELETE CASCADE,
    hits_30d        INTEGER NOT NULL DEFAULT 0,
    last_hit_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_topic_hits_hits_30d ON topic_hits (hits_30d DESC);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0023_organisation',
    'Phase 5 organisation: topics.superseded_by (R6/G5, collision redirects + khipu notes supersede) + topic_hits(slug, hits_30d, last_hit_at) (G6, aggregated from query_log)'
)
ON CONFLICT (version) DO NOTHING;
