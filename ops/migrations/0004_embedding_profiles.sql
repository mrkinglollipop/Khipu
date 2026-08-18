-- Khipu P3 step 3 — vectors for real (2026-08-17).
--
-- The P1 `embeddings` table was shaped for graph nodes only: PK (node_id, chunk_idx)
-- with an FK to nodes(id). Episodes and topics are not nodes, so it cannot hold the
-- memory corpus. This adds a second table with a source-agnostic identity and the
-- plan.md "Embedding profiles" rules built in: every row is tagged with a profile
-- id, profiles are never overwritten in place, and search pins ONE active profile.
--
-- Chunk identity is (profile, kind, ref, chunk_idx):
--   kind ∈ 'episode' | 'topic'      ref = episodes.id::text | topics.slug
-- content_hash lets the backfill / embed-on-capture skip unchanged text
-- (stale detection per plan rule 4).

CREATE TABLE IF NOT EXISTS embedding_profiles (
    id          TEXT PRIMARY KEY,                 -- e.g. 'gemini-embedding-001@768'
    provider    TEXT NOT NULL,                    -- 'gemini' | 'openai-compatible' | 'local'
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    normalize   TEXT NOT NULL DEFAULT 'l2',
    is_active   BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    note        TEXT
);

-- Exactly one active profile at a time (plan rule 2: "one active pointer").
CREATE UNIQUE INDEX IF NOT EXISTS uq_embedding_profiles_one_active
    ON embedding_profiles ((true)) WHERE is_active;

INSERT INTO embedding_profiles (id, provider, model, dim, normalize, is_active, note)
VALUES ('gemini-embedding-001@768', 'gemini', 'gemini-embedding-001', 768, 'l2', true,
        'P1 lock (plan.md Models/LLM); cloud default through P3, local is P4')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS memory_embeddings (
    profile       TEXT NOT NULL REFERENCES embedding_profiles(id),
    kind          TEXT NOT NULL CHECK (kind IN ('episode', 'topic')),
    ref           TEXT NOT NULL,
    chunk_idx     INTEGER NOT NULL DEFAULT 0,
    chunk_text    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,                  -- md5 of the chunk_text that was embedded
    embedding     vector(768) NOT NULL,           -- dim of the current active profile; a new
                                                  -- dim = a new profile = ALTER/new table by design
    built_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile, kind, ref, chunk_idx)
);

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_kind_ref
    ON memory_embeddings (kind, ref);

-- HNSW cosine index, scoped to the active profile so a second profile can be
-- backfilled alongside without polluting the search space (plan rule 1/2).
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_hnsw_gemini768
    ON memory_embeddings USING hnsw (embedding vector_cosine_ops)
    WHERE profile = 'gemini-embedding-001@768';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0004_embedding_profiles',
    'P3 step 3: embedding_profiles (one active) + memory_embeddings (episode/topic chunks, profile-tagged, HNSW cosine)'
)
ON CONFLICT (version) DO NOTHING;
