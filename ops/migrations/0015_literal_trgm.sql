-- Khipu literal-pass speed (R8, Phase 1 — 2026-09-14).
--
-- The ILIKE pass (khipu.cli._search_query / _literal_candidates) sequential-
-- scans episodes/topics/nodes with no supporting index — measured 1.4-4s for
-- this leg alone (docs/plans/2026-09-14-memory-that-works-like-magic.md,
-- finding R8), which is what makes a per-prompt recall lane unaffordable.
-- pg_trgm + a GIN trigram index on the columns that pass actually scans lets
-- Postgres use an index scan instead. Expression indexes on
-- COALESCE(col, '') exist because that is the literal SQL the query uses for
-- a nullable column (topics.title, nodes.name) — a plain-column index does
-- not match that expression and the planner would never pick it up.
--
-- CREATE EXTENSION needs a privileged role. Some hubs' application role
-- cannot create extensions at all; that is a hub-permission fact this
-- migration does not control, so it must degrade to a no-op rather than
-- fail the whole migration run (a permission gap here must never block
-- every migration queued behind it). The reason is visible via NOTICE and
-- (see khipu.drift) a doctor check, never silently swallowed.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'khipu: pg_trgm could not be created (insufficient privilege) — literal search stays a sequential scan; see khipu doctor (literal_trgm_ok)';
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
        CREATE INDEX IF NOT EXISTS idx_episodes_summary_trgm
            ON episodes USING gin (summary gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_topics_title_trgm
            ON topics USING gin ((COALESCE(title, '')) gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_topics_body_trgm
            ON topics USING gin (body gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_nodes_name_trgm
            ON nodes USING gin ((COALESCE(name, '')) gin_trgm_ops);
    ELSE
        RAISE NOTICE 'khipu: pg_trgm not installed — skipping trigram indexes (literal search stays a sequential scan)';
    END IF;
END
$$;

INSERT INTO schema_migrations (version, note)
VALUES (
    '0015_literal_trgm',
    'pg_trgm + GIN trigram indexes on episodes.summary / topics.title,body / nodes.name for the literal ILIKE search pass (R8); degrades to a no-op with a NOTICE if pg_trgm cannot be created'
)
ON CONFLICT (version) DO NOTHING;
