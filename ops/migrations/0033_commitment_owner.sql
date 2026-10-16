-- Owner and origin on every commitment.
-- Scope: docs/plans/2026-10-16-owed-and-ledger-ssot.md (design (a), slice 1,
-- with the owner's decisions 2 and 3).
--
-- Why: in a dispatched job the person typing the prompt is the orchestrator
-- that wrote the brief, so the extractor called the orchestrator "the user"
-- and Khipu stored the job's asks as the user's. With only two owners, every
-- "the orchestrator must", "PR #234's author to" and "the reviewer to" fell to
-- the user too. 97% of one busy project's open items showed as "needs the
-- user".
--
--   origin       'conversation' or 'job'. A capture is a job's when its
--                payload has a parent_session_id (a Claude Code child, a
--                SubagentStop, a runner that set KHIPU_PARENT_SESSION) or its
--                hook was told KHIPU_JOB=1. Unknown stays 'conversation', the
--                behaviour before this column. Existing rows get the default.
--   owner_label  a short name for an owner that is neither the user nor the
--                assistant: "orchestrator", "PR author", "reviewer",
--                "builder", "Codex", "a job". NULL for user and assistant.
--
-- commitments.owner gains a third value, 'agent' (the column is plain text,
-- so no constraint changes). khipu.commitments.resolve_owner_and_label
-- decides it deterministically; a job's question or "Matt to decide X" is the
-- orchestrator's. Agent items never show as "needs the user", are not in
-- Waiting on you, and are not closed when their job's session ends.
--
-- Locks. The ALTERs add a nullable column and a column with a constant
-- default, both catalog-only (no table rewrite), under a 5 second
-- lock_timeout so a busy hub fails the step instead of queueing every
-- capture behind it. The check constraint is added NOT VALID (no scan under
-- the exclusive lock); every existing row already holds the default.
--
-- Additive and safe to run twice. A hub without this migration keeps
-- working: writers and readers check for the columns first (the resolved
-- owner, 'agent' included, is still stored). Reverse with ALTER TABLE
-- commitments DROP CONSTRAINT commitments_origin_check, DROP COLUMN origin,
-- DROP COLUMN owner_label, and deleting the 0033 row from schema_migrations.

SET LOCAL lock_timeout = '5s';

ALTER TABLE commitments ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'conversation';
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS owner_label TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'commitments_origin_check'
                     AND conrelid = 'commitments'::regclass) THEN
        ALTER TABLE commitments ADD CONSTRAINT commitments_origin_check
            CHECK (origin IN ('conversation', 'job')) NOT VALID;
    END IF;
END
$$;

COMMENT ON COLUMN commitments.origin IS
    'Where the commitment was captured: ''conversation'' or ''job'' (a dispatched job: the payload had a parent_session_id, or the hook was told KHIPU_JOB=1). Rows from before migration 0033 are ''conversation''.';
COMMENT ON COLUMN commitments.owner_label IS
    'For owner = ''agent'': which third party owes it ("orchestrator", "PR author", "reviewer", "builder", "Codex", "a job"). NULL for the user and the assistant.';
COMMENT ON COLUMN commitments.owner IS
    'Resolved owner: ''user'' (the user must decide/provide/approve/do it, or it is a question for them), ''assistant'', or ''agent'' (a third party named by owner_label, such as the orchestrator a job reports to). khipu.commitments.resolve_owner_and_label decides it deterministically; the desktop''s "Needs you" section is owner = ''user''.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0033_commitment_owner',
    'commitments.origin (conversation|job) and owner_label; owner gains ''agent'' (a job''s asks are the orchestrator''s)'
)
ON CONFLICT (version) DO NOTHING;
