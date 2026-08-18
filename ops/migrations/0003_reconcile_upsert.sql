-- Khipu P2b — reconcile without truncate (plan.md P2b; audit F1).
-- Adds the two schema facts the non-destructive reconcile needs:
--   1. topics.deleted_at — tombstone. A topic file deliberately deleted from the
--      wiki is marked, never hard-deleted, so the hub-mode reverse-mirror
--      (PG → files) can distinguish "deliberately removed" from "never existed"
--      and will not recreate the file. Purge is explicit-with-confirm only.
--   2. Unique episode identity on (ts, md5(summary)) — verified unique across
--      all 3,883 rows on 2026-08-10 (0 duplicate groups) before this migration.
--      md5(summary) keeps the index entry small; summaries can exceed btree
--      row limits raw. This is what makes the episode upsert idempotent.

ALTER TABLE topics ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

COMMENT ON COLUMN topics.deleted_at IS
    'Tombstone: set by reconcile when the owning topic file was deliberately deleted; cleared on re-appearance. Rows removed only by explicit purge with confirm (plan.md P2b).';

CREATE INDEX IF NOT EXISTS idx_topics_deleted
    ON topics (deleted_at) WHERE deleted_at IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_episodes_ts_summary_md5
    ON episodes (ts, md5(summary));

INSERT INTO schema_migrations (version, note)
VALUES (
    '0003_reconcile_upsert',
    'P2b: topics.deleted_at tombstone + unique (ts, md5(summary)) episode identity for idempotent non-truncating reconcile'
)
ON CONFLICT (version) DO NOTHING;
