-- Khipu — Gemini Embedding 2 profile (inactive until backfill + activate).
--
-- Plan lock: profiles, not overwrite. Keep gemini-embedding-001@768 as the
-- active search pointer until the new profile is fully covered. Same dim 768
-- so memory_embeddings.embedding vector(768) stays; a separate HNSW partial
-- index scopes search to this profile (plan rule 1/2).

INSERT INTO embedding_profiles (id, provider, model, dim, normalize, is_active, note)
VALUES (
    'gemini-embedding-2@768',
    'gemini',
    'gemini-embedding-2',
    768,
    'l2',
    false,
    'Gemini Embedding 2 @768; task prefixes at API time; activate after full memory backfill'
)
ON CONFLICT (id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_hnsw_gemini2_768
    ON memory_embeddings USING hnsw (embedding vector_cosine_ops)
    WHERE profile = 'gemini-embedding-2@768';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0005_gemini_embedding_2',
    'Inactive gemini-embedding-2@768 profile + HNSW partial index; 001 remains active'
)
ON CONFLICT (version) DO NOTHING;
