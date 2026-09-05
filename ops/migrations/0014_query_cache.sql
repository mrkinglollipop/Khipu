-- Khipu query-vector cache (2026-09-05).
--
-- Every Recall / khipu_search in hybrid or semantic mode embeds the question
-- through Gemini before the vector scan. The same question is asked again
-- and again (the hook's recall probe, a person refining a search, the
-- gateway serving several harnesses), and the embedding API is the one leg
-- of search that is rate-limited (HTTP 429 seen 2026-09-05) and slow (a 15 s
-- stall, same day). Cache the vector on the hub, keyed by profile + a hash
-- of the normalised query. Only the hash is stored: no query text lands on
-- the hub through this table.
--
-- Rows unused for khipu.embed.QUERY_CACHE_TTL_DAYS are pruned by the
-- nightly. An un-migrated hub behaves as before (khipu.embed._query_vec
-- checks to_regclass and embeds directly).

CREATE TABLE IF NOT EXISTS memory_query_cache (
    profile       TEXT NOT NULL REFERENCES embedding_profiles(id),
    query_hash    TEXT NOT NULL,
    embedding     vector(768) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    hits          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (profile, query_hash)
);

CREATE INDEX IF NOT EXISTS idx_memory_query_cache_last_used
    ON memory_query_cache (last_used_at);

COMMENT ON TABLE memory_query_cache IS
    'Query vectors for hybrid/semantic search, keyed by embedding profile and an md5 of the normalised query (the text itself is never stored). Pruned by the nightly after QUERY_CACHE_TTL_DAYS without use.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0014_query_cache',
    'memory_query_cache: per-profile query-vector cache so repeated searches cost no embed API call'
)
ON CONFLICT (version) DO NOTHING;
