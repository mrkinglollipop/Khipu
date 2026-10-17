-- When an item's owner was set by hand (a reassign).
-- Scope: docs/plans/2026-10-16-owed-and-ledger-ssot.md (design (c), slice 3).
--
-- Why: a read re-resolves the owner of a 'user' or 'assistant' item from its
-- text (khipu.commitments.row_owner), so moving a question to the assistant
-- would show as the owner's again on the next read. A reassigned owner is
-- kept as written.
--
--   owner_reassigned_at  when khipu_owed_update (action reassign) or
--                        `khipu owed --reassign` last set the owner. NULL:
--                        the owner is resolved from the text as before.
--
-- Locks. One nullable column, catalog-only (no table rewrite), under a 5
-- second lock_timeout so a busy hub fails the step instead of queueing every
-- capture behind it.
--
-- Additive and safe to run twice. A hub without this migration keeps
-- working: a reassign there stores the owner and label, and a read resolves
-- the owner as before. Reverse with ALTER TABLE commitments DROP COLUMN
-- owner_reassigned_at, and deleting the 0035 row from schema_migrations.

SET LOCAL lock_timeout = '5s';

ALTER TABLE commitments ADD COLUMN IF NOT EXISTS owner_reassigned_at TIMESTAMPTZ;

COMMENT ON COLUMN commitments.owner_reassigned_at IS
    'When the owner was last set by hand (a reassign). NULL: the owner is resolved from the text on read.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0035_commitment_owner_reassigned',
    'commitments.owner_reassigned_at (a reassigned owner is kept as written)'
)
ON CONFLICT (version) DO NOTHING;
