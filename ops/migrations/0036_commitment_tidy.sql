-- The one-time clean-up of an Owed list: batches that can be undone.
-- Scope: docs/plans/2026-10-16-owed-and-ledger-ssot.md (design (d), slice 4,
-- with the owner's decision 4).
--
-- Why: one busy project held 3,687 open commitments, 97% of them shown as
-- the user's. `khipu owed tidy` plans a clean-up (keep, reassign, close),
-- and applying a plan writes every change in one batch. Nothing is deleted
-- and every batch can be undone, so each change keeps the item's state from
-- before it.
--
--   commitments.tidy_batch      the batch that last changed this item, or
--                               NULL. Cleared again by an undo.
--   commitment_tidy_batches     one row per applied plan: its project, the
--                               plan's version and time, how the owner gate
--                               passed (NULL when no item was the user's),
--                               the counts, the items skipped because they
--                               changed after planning, and when (and what)
--                               an undo restored or left alone.
--   commitment_tidy_items       one row per changed item: the action (close,
--                               reassign, or a bulk change's reopen or
--                               snooze), the reason, the item's state before
--                               (owner, label, origin, status, the close
--                               fields and the due date), and a fingerprint of its
--                               state after, so an undo leaves alone any
--                               item changed since.
--
-- Locks. One nullable column, catalog-only (no table rewrite), under a 5
-- second lock_timeout so a busy hub fails the step instead of queueing every
-- capture behind it. The two tables are new.
--
-- Additive and safe to run twice. A hub without this migration still plans
-- (the plan writes nothing); applying and undoing refuse and say why.
-- Reverse with DROP TABLE commitment_tidy_items, commitment_tidy_batches,
-- ALTER TABLE commitments DROP COLUMN tidy_batch, and deleting the 0036 row
-- from schema_migrations.

SET LOCAL lock_timeout = '5s';

ALTER TABLE commitments ADD COLUMN IF NOT EXISTS tidy_batch TEXT;

CREATE TABLE IF NOT EXISTS commitment_tidy_batches (
    id            TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    plan_version  INTEGER NOT NULL,
    planned_at    TIMESTAMPTZ,
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    owner_gate    TEXT,
    counts        JSONB NOT NULL DEFAULT '{}'::jsonb,
    skipped       JSONB NOT NULL DEFAULT '[]'::jsonb,
    undone_at     TIMESTAMPTZ,
    undo_report   JSONB
);

CREATE TABLE IF NOT EXISTS commitment_tidy_items (
    batch_id           TEXT NOT NULL REFERENCES commitment_tidy_batches (id),
    commitment_id      BIGINT NOT NULL REFERENCES commitments (id),
    action             TEXT NOT NULL CHECK (action IN ('close', 'reassign', 'reopen', 'snooze')),
    reason             TEXT NOT NULL,
    prior              JSONB NOT NULL,
    after_fingerprint  TEXT NOT NULL,
    undone_at          TIMESTAMPTZ,
    PRIMARY KEY (batch_id, commitment_id)
);

CREATE INDEX IF NOT EXISTS idx_commitment_tidy_items_commitment
    ON commitment_tidy_items (commitment_id);

COMMENT ON COLUMN commitments.tidy_batch IS
    'The `khipu owed tidy` batch that last changed this item (commitment_tidy_batches.id), or NULL.';
COMMENT ON TABLE commitment_tidy_batches IS
    'One applied clean-up plan (khipu.owed_tidy): counts, skipped items, owner gate, undo.';
COMMENT ON TABLE commitment_tidy_items IS
    'One item a clean-up changed: action, reason, its state before (for undo) and a fingerprint of its state after.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0036_commitment_tidy',
    'commitments.tidy_batch, commitment_tidy_batches, commitment_tidy_items (the Owed clean-up, undoable)'
)
ON CONFLICT (version) DO NOTHING;
