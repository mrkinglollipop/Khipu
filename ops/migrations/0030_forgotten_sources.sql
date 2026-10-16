-- Durable forget, hub side: the identities of every forgotten source, and
-- triggers that refuse to bring one back.
-- Scope: docs/plans/2026-10-11-capture-identity.md ("Durable forget: the
-- write gate").
--
-- Why: each Mac keeps a local registry of forgotten sources, and every local
-- writer checks it. A forget made on one Mac never reaches another Mac's
-- registry, and code that predates the registry checks nothing, so either
-- could insert a forgotten source again as a new live row. forget_episode now
-- writes the source's identities here in the same transaction as the
-- tombstone, writers read this table before they insert, and the triggers
-- below refuse any insert or merge that names one, whichever Mac or version
-- sent it.
--
-- identity is a source key or a payload hash, the same text as
-- episode_sources.source_key / payload_sha256 (khipu/capture_identity.py).
-- The natural key formula is 0027's, step 1: sha256("khipu-source-v1" US
-- harness US session_id US transcript_range US window_id US part), US =
-- chr(31), NULL = '', when session_id and transcript_range are non-empty.
--
-- Every check takes pg_advisory_xact_lock(hashtextextended('khipu-source:' ||
-- identity, 0)), the lock forget takes before it inserts the identity, so a
-- writer either commits before the forget publishes (and is a separate
-- episode the forget does not cover) or waits and then sees the identity.
--
-- Additive only: a new table, five new functions, three new triggers and a
-- backfill into the new table. No existing table, column, constraint or row
-- changes. Updates the triggers do not refuse (hygiene setting project or
-- repo_root on a forgotten row, origin stamping, summary edits) behave as
-- before. Safe to run twice: CREATE ... IF NOT EXISTS, CREATE OR REPLACE
-- FUNCTION, DROP TRIGGER IF EXISTS before each CREATE TRIGGER, and every
-- INSERT is ON CONFLICT DO NOTHING.
-- Reverse with DROP TRIGGER khipu_episode_sources_forgotten ON
-- episode_sources, DROP TRIGGER khipu_episodes_forgotten_insert ON episodes,
-- DROP TRIGGER khipu_episodes_forgotten_update ON episodes, DROP FUNCTION for
-- each khipu_* function below, DROP TABLE forgotten_sources and deleting the
-- 0030 row from schema_migrations. Reversing removes the hub-side refusal;
-- each Mac's local registry still fences its own writers.
--
-- Locks: CREATE TRIGGER takes a SHARE ROW EXCLUSIVE lock on its table, so
-- captures wait while this runs; lock_timeout makes a long transaction
-- holding episodes fail this migration instead of queueing every writer
-- behind it. A re-run completes it.

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS forgotten_sources (
    identity      TEXT PRIMARY KEY,
    episode_id    BIGINT,
    forgotten_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE forgotten_sources IS
    'Source keys and payload hashes of forgotten episodes (durable forget). Never deleted by Khipu; the 0030 triggers refuse an insert or merge that names one, and un-forgetting an episode.';

-- Backfill: every source of an episode forgotten before this migration.
INSERT INTO forgotten_sources (identity, episode_id)
SELECT s.source_key, s.episode_id
FROM episode_sources s JOIN episodes e ON e.id = s.episode_id
WHERE e.deleted_at IS NOT NULL
ON CONFLICT (identity) DO NOTHING;

INSERT INTO forgotten_sources (identity, episode_id)
SELECT s.payload_sha256, s.episode_id
FROM episode_sources s JOIN episodes e ON e.id = s.episode_id
WHERE e.deleted_at IS NOT NULL AND s.payload_sha256 IS NOT NULL
ON CONFLICT (identity) DO NOTHING;

CREATE OR REPLACE FUNCTION khipu_natural_source_key(
    harness TEXT, session_id TEXT, transcript_range TEXT, window_id TEXT, part TEXT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN coalesce(session_id, '') <> '' AND coalesce(transcript_range, '') <> ''
        THEN encode(sha256(convert_to(concat_ws(chr(31), 'khipu-source-v1',
                 coalesce(harness, ''), session_id, transcript_range,
                 coalesce(window_id, ''), coalesce(part, '')), 'UTF8')), 'hex')
    END
$$;

-- Raises when any identity is forgotten. Locks are taken in sorted order,
-- the order forget takes them, so a writer and a forget cannot deadlock.
CREATE OR REPLACE FUNCTION khipu_refuse_forgotten(identities TEXT[])
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    k TEXT;
BEGIN
    FOR k IN SELECT DISTINCT x FROM unnest(identities) AS t(x)
             WHERE x IS NOT NULL AND x <> '' ORDER BY x
    LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended('khipu-source:' || k, 0));
        IF EXISTS (SELECT 1 FROM forgotten_sources WHERE identity = k) THEN
            RAISE EXCEPTION 'khipu: forgotten source %', k
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
END
$$;

CREATE OR REPLACE FUNCTION khipu_episode_sources_forgotten()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM khipu_refuse_forgotten(ARRAY[NEW.source_key, NEW.payload_sha256]);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION khipu_episodes_forgotten_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM khipu_refuse_forgotten(ARRAY[khipu_natural_source_key(
        coalesce(NEW.harness, NEW.raw->>'harness'),
        NEW.session_id,
        coalesce(NEW.transcript_range, NEW.raw->>'transcript_range'),
        coalesce(NEW.window_id, NEW.raw->>'window_id'),
        NEW.raw->>'part')]);
    RETURN NEW;
END
$$;

-- Refuses un-forgetting, and a merge receipt for a forgotten source. Every
-- other update passes, including updates to a forgotten row's metadata.
CREATE OR REPLACE FUNCTION khipu_episodes_forgotten_update()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    before_merge JSONB;
    entry JSONB;
BEGIN
    IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL THEN
        RAISE EXCEPTION 'khipu: forgotten episode % cannot be restored', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    IF jsonb_typeof(NEW.raw->'merged_from') = 'array'
       AND (NEW.raw->'merged_from') IS DISTINCT FROM (OLD.raw->'merged_from') THEN
        before_merge := CASE WHEN jsonb_typeof(OLD.raw->'merged_from') = 'array'
                             THEN OLD.raw->'merged_from' ELSE '[]'::jsonb END;
        FOR entry IN SELECT value FROM jsonb_array_elements(NEW.raw->'merged_from')
        LOOP
            CONTINUE WHEN jsonb_typeof(entry) <> 'object'
                       OR before_merge @> jsonb_build_array(entry);
            PERFORM khipu_refuse_forgotten(ARRAY[
                nullif(entry->>'source_id', ''),
                khipu_natural_source_key(
                    entry->'payload'->>'harness',
                    coalesce(entry->'payload'->>'session_id', entry->>'session_id'),
                    entry->'payload'->>'transcript_range',
                    entry->'payload'->>'window_id',
                    entry->'payload'->>'part')]);
        END LOOP;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS khipu_episode_sources_forgotten ON episode_sources;
CREATE TRIGGER khipu_episode_sources_forgotten
    BEFORE INSERT ON episode_sources
    FOR EACH ROW EXECUTE FUNCTION khipu_episode_sources_forgotten();

DROP TRIGGER IF EXISTS khipu_episodes_forgotten_insert ON episodes;
CREATE TRIGGER khipu_episodes_forgotten_insert
    BEFORE INSERT ON episodes
    FOR EACH ROW EXECUTE FUNCTION khipu_episodes_forgotten_insert();

DROP TRIGGER IF EXISTS khipu_episodes_forgotten_update ON episodes;
CREATE TRIGGER khipu_episodes_forgotten_update
    BEFORE UPDATE ON episodes
    FOR EACH ROW EXECUTE FUNCTION khipu_episodes_forgotten_update();

INSERT INTO schema_migrations (version, note)
VALUES (
    '0030_forgotten_sources',
    'Durable forget: forgotten_sources (identity PK, episode_id, forgotten_at), backfilled from forgotten episodes'' source rows; triggers refuse inserting a forgotten source into episodes or episode_sources, merging one into an episode, and un-forgetting an episode'
)
ON CONFLICT (version) DO NOTHING;
