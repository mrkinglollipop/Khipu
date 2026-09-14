-- Khipu freshness within a minute (R7, 2026-09-14 "memory that works like magic").
--
-- "Nd old" and the recency ranking read `updated_at`/`created_at` — the
-- timestamp of when Khipu last INGESTED a row, not when the row's own
-- content actually happened. A note file mirrored today reads as "0 days
-- old" even when its own frontmatter (or its mtime) says it has not been
-- touched in a month. `event_at` is the source's own timestamp; the
-- existing ingest-time columns (`topics.updated_at`, `episodes.ingested_at`)
-- keep meaning what they always meant.
--
-- topics.event_at: nullable, populated by khipu.mirror._upsert_topic going
-- forward (frontmatter/metadata date, else the file's own mtime, never
-- now()) — existing note: topics get it via khipu.notes.backfill_event_at()
-- (one-time, idempotent, run by the deploying session, not by this
-- migration: it needs to re-read each note file from disk, which SQL alone
-- cannot do).
--
-- episodes.event_at: a GENERATED column mirroring `ts` (the timestamp an
-- episode already carries for when its conversation happened, as opposed to
-- `ingested_at`, when Khipu wrote the row) — always in sync, no backfill or
-- write-path change needed.

ALTER TABLE topics ADD COLUMN IF NOT EXISTS event_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_topics_event_at ON topics (event_at DESC);

ALTER TABLE episodes ADD COLUMN IF NOT EXISTS event_at TIMESTAMPTZ GENERATED ALWAYS AS (ts) STORED;

CREATE INDEX IF NOT EXISTS idx_episodes_event_at ON episodes (event_at DESC);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0022_event_at',
    'Phase 4 freshness R7: topics.event_at (nullable, app-populated, never now()) + episodes.event_at (generated from ts) — the source''s own timestamp, distinct from ingest time'
)
ON CONFLICT (version) DO NOTHING;
