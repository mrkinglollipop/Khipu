-- Khipu — native media embeddings under the active Gemini Embedding 2 profile.
--
-- Widens memory_embeddings.kind to include 'media' (episode | topic | media).
-- media_assets holds file identity (path, sha256, mime); memory_embeddings.ref
-- is the media_assets.id (polymorphic, same pattern as episode/topic refs).
--
-- For kind=media: content_hash is sha256 of the file bytes (not md5 of chunk_text).
-- chunk_text stores a display label (relative path / filename), never pixels.
-- Same vector(768) column + existing gemini-embedding-2@768 HNSW partial index.

ALTER TABLE memory_embeddings
    DROP CONSTRAINT IF EXISTS memory_embeddings_kind_check;

ALTER TABLE memory_embeddings
    ADD CONSTRAINT memory_embeddings_kind_check
    CHECK (kind IN ('episode', 'topic', 'media'));

CREATE TABLE IF NOT EXISTS media_assets (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    path        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    mime        TEXT NOT NULL,
    bytes       BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_media_assets_source_path
    ON media_assets (source_id, path);

CREATE INDEX IF NOT EXISTS idx_media_assets_sha256
    ON media_assets (sha256);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0006_memory_embeddings_media',
    'Widen memory_embeddings.kind to media; media_assets table for native Gemini Embedding 2 image ingest'
)
ON CONFLICT (version) DO NOTHING;
