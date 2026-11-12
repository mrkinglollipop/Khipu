-- Exact source bytes are separate from the redacted, body-only topic revisions.
SET LOCAL lock_timeout = '5s';
CREATE TABLE IF NOT EXISTS wiki_writes (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    written_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    before_bytes BYTEA,
    after_hash TEXT NOT NULL,
    revision_id BIGINT NOT NULL REFERENCES topic_revisions(id),
    episode_ids BIGINT[] NOT NULL,
    undone_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_wiki_writes_slug ON wiki_writes (slug, id DESC);
CREATE INDEX IF NOT EXISTS idx_wiki_writes_run ON wiki_writes (run_id);
CREATE TABLE IF NOT EXISTS wiki_holds (
    slug TEXT PRIMARY KEY,
    until_at TIMESTAMPTZ NOT NULL
);
INSERT INTO schema_migrations (version, note)
VALUES ('0054_wiki_writes', 'Exact nightly wiki undo provenance and expiring holds')
ON CONFLICT (version) DO NOTHING;
