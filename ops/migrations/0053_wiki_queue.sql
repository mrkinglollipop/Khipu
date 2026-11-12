-- Draft-first nightly wiki groundwork; no backfill and no live writer.
SET LOCAL lock_timeout = '5s';
CREATE TABLE IF NOT EXISTS wiki_queue (
    slug TEXT NOT NULL,
    episode_id BIGINT NOT NULL REFERENCES episodes(id),
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at TIMESTAMPTZ,
    PRIMARY KEY (slug, episode_id)
);
-- No topics FK: a proposed new slug may enter the queue before its page exists.
CREATE INDEX IF NOT EXISTS idx_wiki_queue_waiting
    ON wiki_queue (queued_at, slug) WHERE done_at IS NULL;
-- Decisions are kept even when an opened page is later retired by undo.
CREATE TABLE IF NOT EXISTS wiki_slugs (
    slug TEXT PRIMARY KEY,
    decision TEXT NOT NULL CHECK (decision IN ('page', 'tag', 'alias')),
    target TEXT,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((decision = 'alias' AND target IS NOT NULL)
        OR (decision IN ('page', 'tag') AND target IS NULL))
);
INSERT INTO schema_migrations (version, note)
VALUES ('0053_wiki_queue', 'Nightly wiki queue; assignment and drafts remain read-only')
ON CONFLICT (version) DO NOTHING;
