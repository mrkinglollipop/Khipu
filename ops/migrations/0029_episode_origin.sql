-- Gateway provenance, phase 1: a server-stamped owner for every episode.
-- Scope: docs/plans/2026-10-11-gateway-provenance.md (decisions 1 and 4; phase 1).
--
-- episodes.origin is written by the server, never taken from a payload:
--   gateway:<token label>  written through the HTTPS gateway
--   local:<12 hex>         written by the CLI or a hook on a machine, where the
--                          hex is the start of sha256 of that machine's hardware
--                          id (khipu/machine_identity.py)
--   NULL                   unknown; mutable only locally
--
-- Backfill (decision 4), by session_id prefix only:
--   grokbot: and grok_bot:                       -> gateway:default
--   claude_code: cursor: codex: aegis:           -> this machine's local origin
--   anything else, or no session_id              -> stays NULL
-- "This machine" is the parameter khipu.local_origin, which `khipu migrate`
-- sets for this transaction from khipu/machine_identity.py. Paths that may run
-- on a machine other than the hub owner's (setup against an existing database,
-- `khipu join import`) set it to 'none': the column, the index and the gateway
-- backfill still apply, and the local rows stay NULL rather than be claimed by
-- the wrong machine. Unset or malformed, the migration stops instead of
-- guessing. Each step is one DO block that refuses first, so a refusal
-- applies nothing even under a psql that continues past errors.
--
-- Additive: one nullable column, one partial index, an UPDATE of NULL rows
-- only. Safe to run twice. Reverse with DROP INDEX idx_episodes_origin, ALTER
-- TABLE episodes DROP COLUMN origin and deleting the 0029 row from
-- schema_migrations; dropping the column destroys every stamped origin.
--
-- Locks. The live hub takes traffic while this runs, so each step sets a
-- lock_timeout: a lock that is not granted within 5 seconds (a long
-- transaction holding episodes) fails the step instead of queueing behind it
-- and blocking every reader and writer queued after it. `khipu migrate` runs
-- each step section (from one top-level khipu:step marker line to the next)
-- as its own transaction, so the ACCESS EXCLUSIVE lock of step 1 (a
-- catalog-only ALTER: no default, no rewrite) is released before the index
-- build (step 2, a SHARE lock) and the backfill (step 3, row
-- locks only). The version is recorded in step 3 only, so a step that fails,
-- on a lock timeout or anything else, leaves the version unrecorded: step 1
-- failing changes nothing, a later step failing leaves at most the empty
-- column (which writers already handle as a hub without the backfill), and a
-- re-run completes the migration. Run as one file (psql, the scratch tests),
-- the sections are one transaction, as before.

-- khipu:step 1: refuse without an identity, then the catalog-only column.
SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    local_origin TEXT := current_setting('khipu.local_origin', true);
BEGIN
    IF local_origin IS NULL
       OR (local_origin <> 'none' AND local_origin !~ '^local:[0-9a-f]{12}$') THEN
        RAISE EXCEPTION '0029_episode_origin needs this machine''s identity in khipu.local_origin; apply it with `khipu migrate`';
    END IF;

    ALTER TABLE episodes ADD COLUMN IF NOT EXISTS origin TEXT;

    COMMENT ON COLUMN episodes.origin IS
        'Server-stamped owner: gateway:<token label>, local:<machine hash>, or NULL (unknown, mutable only locally). Never taken from a capture payload.';
END
$$;

-- khipu:step 2: the partial index, under a SHARE lock (writes wait, reads do not).
SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    local_origin TEXT := current_setting('khipu.local_origin', true);
BEGIN
    IF local_origin IS NULL
       OR (local_origin <> 'none' AND local_origin !~ '^local:[0-9a-f]{12}$') THEN
        RAISE EXCEPTION '0029_episode_origin needs this machine''s identity in khipu.local_origin; apply it with `khipu migrate`';
    END IF;

    CREATE INDEX IF NOT EXISTS idx_episodes_origin
        ON episodes (origin) WHERE origin IS NOT NULL;
END
$$;

-- khipu:step 3: the backfill, then the version.
SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    local_origin TEXT := current_setting('khipu.local_origin', true);
BEGIN
    IF local_origin IS NULL
       OR (local_origin <> 'none' AND local_origin !~ '^local:[0-9a-f]{12}$') THEN
        RAISE EXCEPTION '0029_episode_origin needs this machine''s identity in khipu.local_origin; apply it with `khipu migrate`';
    END IF;

    UPDATE episodes
    SET origin = 'gateway:default'
    WHERE origin IS NULL
      AND position(':' IN coalesce(session_id, '')) > 0
      AND split_part(session_id, ':', 1) IN ('grokbot', 'grok_bot');

    IF local_origin <> 'none' THEN
        UPDATE episodes
        SET origin = local_origin
        WHERE origin IS NULL
          AND position(':' IN coalesce(session_id, '')) > 0
          AND split_part(session_id, ':', 1) IN ('claude_code', 'cursor', 'codex', 'aegis');
    END IF;

    INSERT INTO schema_migrations (version, note)
    VALUES (
        '0029_episode_origin',
        'Gateway provenance phase 1: episodes.origin (gateway:<label> | local:<machine hash> | NULL) + partial index; backfilled by session_id prefix'
    )
    ON CONFLICT (version) DO NOTHING;
END
$$;
