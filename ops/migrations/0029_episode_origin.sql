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
-- guessing. Everything runs inside one DO block so that a refusal applies
-- nothing even under a psql that continues past errors.
--
-- Additive: one nullable column, one partial index, an UPDATE of NULL rows
-- only. Safe to run twice. Reverse with DROP INDEX idx_episodes_origin, ALTER
-- TABLE episodes DROP COLUMN origin and deleting the 0029 row from
-- schema_migrations.

DO $$
DECLARE
    local_origin TEXT := current_setting('khipu.local_origin', true);
BEGIN
    IF local_origin IS NULL
       OR (local_origin <> 'none' AND local_origin !~ '^local:[0-9a-f]{12}$') THEN
        RAISE EXCEPTION '0029_episode_origin needs this machine''s identity in khipu.local_origin; apply it with `khipu migrate`';
    END IF;

    ALTER TABLE episodes ADD COLUMN IF NOT EXISTS origin TEXT;

    CREATE INDEX IF NOT EXISTS idx_episodes_origin
        ON episodes (origin) WHERE origin IS NOT NULL;

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

    COMMENT ON COLUMN episodes.origin IS
        'Server-stamped owner: gateway:<token label>, local:<machine hash>, or NULL (unknown, mutable only locally). Never taken from a capture payload.';

    INSERT INTO schema_migrations (version, note)
    VALUES (
        '0029_episode_origin',
        'Gateway provenance phase 1: episodes.origin (gateway:<label> | local:<machine hash> | NULL) + partial index; backfilled by session_id prefix'
    )
    ON CONFLICT (version) DO NOTHING;
END
$$;
