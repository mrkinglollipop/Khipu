-- A change stamp on every episode row, for the replica's incremental pull.
-- Scope: docs/plans/2026-10-16-offline-and-self-healing.md (design A: an
-- incremental pull of episodes written since the replica's cursors).
--
-- Why: the pull read episodes by ingested_at, which is set once. A capture
-- merged into an older episode (capture._merge_into) edits that row in place
-- and keeps its ingest time, so the pull never saw the new memory and still
-- reported success. episodes.changed_at moves on every insert and on every
-- update that changes the row, and the pull reads by it.
--
-- The stamp is clock_timestamp(), set by a BEFORE trigger, so no writer has
-- to remember it. A long transaction can still commit a stamp older than one
-- already read; the pull's existing overlap (it never moves its cursor past
-- the hub's now minus a few minutes) re-reads those, as it did for
-- ingested_at. Rows from before this migration keep changed_at NULL until
-- their next change; the pull reads those by ingested_at, as before.
--
-- Locks. The live hub takes traffic while this runs, so each step sets a
-- lock_timeout: a lock not granted within 5 seconds fails the step instead
-- of queueing every capture behind it. Step 1 takes ACCESS EXCLUSIVE on
-- episodes first, the strongest lock it needs (ADD COLUMN; CREATE TRIGGER
-- needs less), so it never upgrades a lock it holds (the 0030 deadlock
-- lesson). The column has no default, so the ALTER is catalog-only and the
-- lock is held for milliseconds. `khipu migrate` runs each khipu:step section
-- as its own transaction: the index build (step 2) holds only a SHARE lock,
-- after step 1's lock is released. The version is recorded in step 2, so a
-- failed step leaves it unrecorded and a re-run completes the migration.
--
-- Additive: one nullable column, one function, one trigger, one partial
-- index. No existing row changes. Safe to run twice. Reverse with DROP
-- TRIGGER khipu_episodes_changed_at ON episodes, DROP FUNCTION
-- khipu_episodes_changed_at(), DROP INDEX idx_episodes_changed_at, ALTER
-- TABLE episodes DROP COLUMN changed_at and deleting the 0032 row from
-- schema_migrations; the pull then falls back to ingested_at.

-- khipu:step 1: the column, the function and the trigger.
SET LOCAL lock_timeout = '5s';

LOCK TABLE episodes IN ACCESS EXCLUSIVE MODE;

ALTER TABLE episodes ADD COLUMN IF NOT EXISTS changed_at TIMESTAMPTZ;

COMMENT ON COLUMN episodes.changed_at IS
    'When the row last changed (insert or any update that changes it), set by the khipu_episodes_changed_at trigger. The replica pull reads by it. NULL: unchanged since before migration 0032.';

CREATE OR REPLACE FUNCTION khipu_episodes_changed_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN
        RETURN NEW;
    END IF;
    NEW.changed_at := clock_timestamp();
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS khipu_episodes_changed_at ON episodes;
CREATE TRIGGER khipu_episodes_changed_at
    BEFORE INSERT OR UPDATE ON episodes
    FOR EACH ROW EXECUTE FUNCTION khipu_episodes_changed_at();

-- khipu:step 2: the partial index, under a SHARE lock (writes wait, reads do not), then the version.
SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS idx_episodes_changed_at
    ON episodes (changed_at, id) WHERE changed_at IS NOT NULL;

INSERT INTO schema_migrations (version, note)
VALUES (
    '0032_episode_changed_at',
    'episodes.changed_at, stamped by a BEFORE INSERT OR UPDATE trigger on every change, + partial index; the replica pull reads by it'
)
ON CONFLICT (version) DO NOTHING;
