-- Library sources and bring-your-own embeddings (Session A).
-- Scope: docs/plans/2026-10-07-library-sources-byoe.md
--
-- 1. embedding_profiles.endpoint: the base URL of an `openai-compatible`
--    profile (OpenAI, Ollama, LM Studio, any /v1/embeddings server). Null for
--    gemini and voyage, whose endpoints are fixed in code. The endpoint lives
--    on the profile record, not in the models.embed config.
-- 2. memory_embeddings.embedding and memory_query_cache.embedding become an
--    UNTYPED `vector` (no dimension), so a memory profile of any width can be
--    re-embedded alongside the 768-wide Gemini ones. vector(768) -> vector
--    keeps every stored value; the cast only drops the dimension constraint.
--    The two Gemini HNSW indexes from 0004/0005 are dropped and recreated
--    under the same names as expression + partial indexes (below). The ALTER
--    takes an ACCESS EXCLUSIVE lock on memory_embeddings and the index builds
--    re-read every Gemini row, so apply it when nothing is capturing; the
--    block is a no-op on a hub that already ran it.
-- 3. library_sources / library_documents / library_chunks / library_embeddings:
--    a folder of .txt/.md files indexed as a search space of its own. Each
--    source names the embedding profile it is embedded with, so the memory
--    space keeps its single active pointer. library_embeddings.embedding is
--    untyped too.
--
-- pgvector cannot index an untyped column directly, and a migration cannot
-- know the dimensions of profiles that do not exist yet, so the only indexes
-- built here are the two existing Gemini ones. Every other profile's index is
-- created per profile, by code, when the profile first gets rows or becomes
-- active (khipu.profiles.ensure_profile_index, for memory_embeddings and
-- library_embeddings alike):
--
--   CREATE INDEX IF NOT EXISTS <idx name>
--       ON <table> USING hnsw ((embedding::vector(<dim>)) vector_cosine_ops)
--       WHERE profile = '<id>';
--
-- pgvector README: an expression index with a cast is the documented way to
-- index a column without fixed dimensions (section "Frequently Asked Questions":
-- "Can I store vectors with different dimensions in the same column?" ->
-- `CREATE INDEX ON embeddings USING hnsw ((embedding::vector(3)) vector_l2_ops)
-- WHERE (model_id = 123)`), and a trailing WHERE makes it a partial index
-- (section "Filtering"). A query uses it only when it repeats both the cast
-- and the predicate:
--   ... WHERE profile = '<id>' ORDER BY embedding::vector(<dim>) <=> $1 LIMIT n
-- pgvector indexes at most 2000 dimensions on vector(n).
--
-- NOT applied to any hub by this change: a person applies it after
-- authorization.

ALTER TABLE embedding_profiles ADD COLUMN IF NOT EXISTS endpoint TEXT;

-- Memory space: untyped vectors + expression indexes for the two Gemini profiles.
-- Guarded so a rerun (or a hub without these tables, e.g. the pgvector-free
-- scratch database) skips it; the CREATE INDEX statements that follow are
-- IF NOT EXISTS and need pgvector, like 0004/0005 did.
DO $$
BEGIN
    IF to_regclass('public.memory_embeddings') IS NOT NULL
       AND (SELECT a.atttypmod FROM pg_attribute a
             WHERE a.attrelid = to_regclass('public.memory_embeddings')
               AND a.attname = 'embedding') <> -1 THEN
        DROP INDEX IF EXISTS idx_memory_embeddings_hnsw_gemini768;
        DROP INDEX IF EXISTS idx_memory_embeddings_hnsw_gemini2_768;
        ALTER TABLE memory_embeddings ALTER COLUMN embedding TYPE vector;
    END IF;
    IF to_regclass('public.memory_query_cache') IS NOT NULL
       AND (SELECT a.atttypmod FROM pg_attribute a
             WHERE a.attrelid = to_regclass('public.memory_query_cache')
               AND a.attname = 'embedding') <> -1 THEN
        ALTER TABLE memory_query_cache ALTER COLUMN embedding TYPE vector;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_hnsw_gemini768
    ON memory_embeddings USING hnsw ((embedding::vector(768)) vector_cosine_ops)
    WHERE profile = 'gemini-embedding-001@768';

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_hnsw_gemini2_768
    ON memory_embeddings USING hnsw ((embedding::vector(768)) vector_cosine_ops)
    WHERE profile = 'gemini-embedding-2@768';

CREATE TABLE IF NOT EXISTS library_sources (
    name        TEXT PRIMARY KEY,
    root        TEXT NOT NULL,
    profile     TEXT NOT NULL REFERENCES embedding_profiles(id),
    enabled     BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS library_documents (
    id            SERIAL PRIMARY KEY,
    source        TEXT NOT NULL REFERENCES library_sources(name) ON DELETE CASCADE,
    rel_path      TEXT NOT NULL,
    title         TEXT,
    author        TEXT,
    tags          TEXT[] NOT NULL DEFAULT '{}',
    bytes         BIGINT NOT NULL DEFAULT 0,
    mtime         TIMESTAMPTZ,
    content_hash  TEXT NOT NULL,
    scanned_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, rel_path)
);

CREATE TABLE IF NOT EXISTS library_chunks (
    document      INTEGER NOT NULL REFERENCES library_documents(id) ON DELETE CASCADE,
    chunk_idx     INTEGER NOT NULL,
    chunk_text    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    PRIMARY KEY (document, chunk_idx)
);

CREATE TABLE IF NOT EXISTS library_embeddings (
    profile     TEXT NOT NULL REFERENCES embedding_profiles(id),
    document    INTEGER NOT NULL,
    chunk_idx   INTEGER NOT NULL,
    embedding   vector NOT NULL,
    built_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile, document, chunk_idx),
    FOREIGN KEY (document, chunk_idx)
        REFERENCES library_chunks (document, chunk_idx) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_library_embeddings_doc
    ON library_embeddings (document, chunk_idx);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0026_library',
    'Library sources: embedding_profiles.endpoint; memory_embeddings/memory_query_cache/library_embeddings take any vector width (Gemini indexes recreated as expression indexes; other profiles indexed by code); library_sources/documents/chunks/embeddings'
)
ON CONFLICT (version) DO NOTHING;
