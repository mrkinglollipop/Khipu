-- The writer's own clock for each ledger history entry, kept apart from when
-- the entry landed. Scope: docs/plans/2026-10-15-work-view.md ("Many writers
-- at once", "When the hub is away").
--
-- Why: a history entry's ``at`` and the row's ``updated_at`` came from the
-- writer's clock when the write was made, so a write that waited in a Mac's
-- outbox (or behind a busy hub) landed looking older than writes that landed
-- before it. They now come from the hub's clock under the project's ledger
-- lock, in landing order, and the writer's time is kept here.
--
--   written_at   when the writer made the write, by its own clock; NULL for
--                entries written before this migration and for imports.
--
-- Locks. One nullable column, catalog-only (no table rewrite), under a 5
-- second lock_timeout so a busy hub fails the step instead of queueing every
-- ledger write behind it.
--
-- Additive and safe to run twice. A hub without this migration keeps
-- working: writes land with the hub's time as ``at`` and the writer's time is
-- not kept. Reverse with ALTER TABLE ledger_history DROP COLUMN written_at,
-- and deleting the 0038 row from schema_migrations.

SET LOCAL lock_timeout = '5s';

ALTER TABLE ledger_history ADD COLUMN IF NOT EXISTS written_at TIMESTAMPTZ;

COMMENT ON COLUMN ledger_history.written_at IS
    'When the writer made this write, by its own clock; at is when it landed, by the hub''s.';

INSERT INTO schema_migrations (version, note)
VALUES ('0038_ledger_written_at', 'ledger_history.written_at: the writer''s clock, kept apart from the landing time')
ON CONFLICT (version) DO NOTHING;
