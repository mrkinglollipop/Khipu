-- Decoded owner words, indexed without rewriting episodes or blocking captures.
-- This SQL wrapper is parallel safe; no correlated JSON subplans in retrieval.
-- It runs on every capture write, so path errors are silenced: odd stored data
-- must never fail a capture.
CREATE OR REPLACE FUNCTION public.khipu_owner_words(value jsonb)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
  SELECT lower(COALESCE(string_agg(v #>> '{}', chr(10)), ''))
  FROM jsonb_path_query(value, 'strict $.** ? (@.type() == "string")', '{}', true) AS v
$fn$;
-- 0015 supplies pg_trgm. If it was unavailable, fail explicitly rather than
-- record a migration whose required index was never created.
-- khipu:step autocommit
-- khipu:expect CREATE INDEX idx_episodes_owner_words_trgm ON public.episodes USING gin (khipu_owner_words(verbatim) gin_trgm_ops)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_episodes_owner_words_trgm
    ON public.episodes USING gin (public.khipu_owner_words(verbatim) gin_trgm_ops);
