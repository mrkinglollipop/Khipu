-- Who set an item's owner, why, and what it was before; similar items kept apart.
-- Scope: docs/plans/2026-10-16-owed-and-ledger-ssot.md (design (c), slices 1 and 3).
--
-- Why: a reassign kept only its time (0035), so a Mac's copy could not show
-- who moved an item or from whom, and an offline read could resolve the owner
-- differently from the hub. Dedup also folded the owner's new promise into a
-- similar item someone else owed, losing his ownership and his wording.
--
--   owner_reassigned_by    who set the owner by hand: the tool or command
--                          (khipu_owed_update, khipu owed, tidy).
--   owner_reassign_reason  the reason given with that change, if any.
--   owner_prior            the owner before the change.
--   owner_prior_label      that owner's label (agent items only).
--   linked_ids             ids of open items another owner owes that say
--                          the same thing. Both stay; each names the other.
--   latest_text            the newest wording of a restated item, when a
--                          later capture said the same thing in other words.
--
-- Locks. Nullable columns plus one with a constant default, catalog-only
-- (no table rewrite), under a 5 second lock_timeout so a busy hub fails the
-- step instead of queueing every capture behind it.
--
-- Additive and safe to run twice. A hub without this migration keeps
-- working: a reassign stores the owner and its time as before, and capture
-- still keeps similar items of different owners apart, without the link.
-- Reverse with ALTER TABLE commitments DROP COLUMN for each column, and
-- deleting the 0037 row from schema_migrations.

SET LOCAL lock_timeout = '5s';

ALTER TABLE commitments ADD COLUMN IF NOT EXISTS owner_reassigned_by TEXT;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS owner_reassign_reason TEXT;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS owner_prior TEXT;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS owner_prior_label TEXT;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS linked_ids BIGINT[] NOT NULL DEFAULT '{}';
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS latest_text TEXT;

COMMENT ON COLUMN commitments.owner_reassigned_by IS
    'Who last set the owner by hand (the tool or command), or NULL.';
COMMENT ON COLUMN commitments.owner_reassign_reason IS
    'The reason given with the last hand-set owner, or NULL.';
COMMENT ON COLUMN commitments.owner_prior IS
    'The owner before the last hand-set change, or NULL.';
COMMENT ON COLUMN commitments.owner_prior_label IS
    'The label of the owner before the last hand-set change, or NULL.';
COMMENT ON COLUMN commitments.linked_ids IS
    'Open items of another owner that say the same thing; both are kept.';
COMMENT ON COLUMN commitments.latest_text IS
    'The newest wording of this item when a later capture restated it in other words.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0037_commitment_owner_lineage',
    'commitments reassignment metadata (by, reason, prior owner), linked_ids, latest_text'
)
ON CONFLICT (version) DO NOTHING;
