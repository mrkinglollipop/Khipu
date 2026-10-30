-- Trigram indexes for the episode literal (ILIKE) leg. The five-column
-- episode ILIKE in khipu.cli._search_query was a sequential scan because only
-- episodes.summary had a trigram index (migration 0015); an OR across an
-- indexed and an unindexed column cannot use a BitmapOr. These four expression
-- indexes match the query text exactly (COALESCE(col::text, '')), so the
-- planner can BitmapOr all five. Measured on a hermetic hub-shaped copy
-- (23,600 episodes): literal p50 141 ms -> 8 ms, capture insert +0.24 ms.
-- Scope: docs/plans/2026-10-19-swarm-ready.md, slice 4.
--
-- No project trigram index: the substring project filter measured 13 ms at
-- hub size, under the 50 ms bar the scope sets.
--
-- Each index is built CONCURRENTLY in its own autocommit step (one statement
-- per step; see khipu/migrate.py). "khipu:requires pg_trgm" skips a step
-- when 0015 could not create the extension (insufficient privilege): literal
-- search then stays a sequential scan, as 0015 documents, rather than the
-- migration failing. The last step records the version.
-- khipu:step autocommit
-- khipu:requires pg_trgm
-- khipu:expect CREATE INDEX idx_episodes_topics_text_trgm ON public.episodes USING gin (COALESCE((topics)::text, ''::text) gin_trgm_ops)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_episodes_topics_text_trgm ON episodes USING gin ((COALESCE(topics::text, '')) gin_trgm_ops);
-- khipu:step autocommit
-- khipu:requires pg_trgm
-- khipu:expect CREATE INDEX idx_episodes_decisions_text_trgm ON public.episodes USING gin (COALESCE((decisions)::text, ''::text) gin_trgm_ops)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_episodes_decisions_text_trgm ON episodes USING gin ((COALESCE(decisions::text, '')) gin_trgm_ops);
-- khipu:step autocommit
-- khipu:requires pg_trgm
-- khipu:expect CREATE INDEX idx_episodes_preferences_text_trgm ON public.episodes USING gin (COALESCE((preferences)::text, ''::text) gin_trgm_ops)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_episodes_preferences_text_trgm ON episodes USING gin ((COALESCE(preferences::text, '')) gin_trgm_ops);
-- khipu:step autocommit
-- khipu:requires pg_trgm
-- khipu:expect CREATE INDEX idx_episodes_people_text_trgm ON public.episodes USING gin (COALESCE((people)::text, ''::text) gin_trgm_ops)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_episodes_people_text_trgm ON episodes USING gin ((COALESCE(people::text, '')) gin_trgm_ops);
