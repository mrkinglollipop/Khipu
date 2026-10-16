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
-- The hub decides at the episode row. A current writer names the identities
-- of the row it is inserting in the transaction-local setting
-- khipu.source_identities (khipu/tombstones.py, announce_hub_identities), and
-- the episodes insert trigger refuses the row itself when one is forgotten.
-- It also holds each identity's advisory lock until commit, so a forget
-- cannot land between the episode row and its source row, and no savepoint
-- around the source row can leave a live episode behind. A writer that names
-- nothing (older code) is still refused on the natural key.
--
-- Order, all in this file's one transaction: lock both tables, create the
-- table and the triggers, then backfill. Both tables are locked ACCESS
-- EXCLUSIVE first, the lock DROP TRIGGER needs, so the migration never
-- upgrades a lock it holds. An earlier version took SHARE ROW EXCLUSIVE and
-- upgraded at DROP TRIGGER; a merge writer holding its target row lock (FOR
-- UPDATE) then waited on the migration's lock while the migration waited on
-- the row lock, and the hub aborted one of them as a deadlock. The migration
-- waits for episodes while it holds nothing, then asks for episode_sources
-- without waiting; if a writer holds that, it lets go of episodes and tries
-- again, so it never waits while holding a lock a writer needs. The locks
-- block reads too, for as long as the backfill runs; no forget can commit
-- between the backfill's scan and the triggers going live, so the backfill
-- sees every forget committed before this migration, and every later forget
-- runs under the triggers. lock_timeout and the 5 s deadline make a long
-- transaction holding either table fail this migration instead of queueing
-- every capture behind it; khipu migrate retries it from the start.
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

SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    deadline TIMESTAMPTZ := clock_timestamp() + interval '5 seconds';
BEGIN
    LOOP
        BEGIN
            LOCK TABLE episodes IN ACCESS EXCLUSIVE MODE;
            LOCK TABLE episode_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
            RETURN;
        EXCEPTION WHEN lock_not_available THEN
            -- Leaving the block released whatever it took.
            IF clock_timestamp() >= deadline THEN
                RAISE;
            END IF;
            PERFORM pg_sleep(0.02);
        END;
    END LOOP;
END
$$;

CREATE TABLE IF NOT EXISTS forgotten_sources (
    identity      TEXT PRIMARY KEY,
    episode_id    BIGINT,
    forgotten_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE forgotten_sources IS
    'Source keys and payload hashes of forgotten episodes (durable forget). Never deleted by Khipu; the 0030 triggers refuse an insert or merge that names one, and un-forgetting an episode.';

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

-- The natural key from the row, plus every identity the writer named.
CREATE OR REPLACE FUNCTION khipu_episodes_forgotten_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM khipu_refuse_forgotten(
        ARRAY[khipu_natural_source_key(
            coalesce(NEW.harness, NEW.raw->>'harness'),
            NEW.session_id,
            coalesce(NEW.transcript_range, NEW.raw->>'transcript_range'),
            coalesce(NEW.window_id, NEW.raw->>'window_id'),
            NEW.raw->>'part')]
        || coalesce(string_to_array(
               nullif(current_setting('khipu.source_identities', true), ''), ','),
           ARRAY[]::TEXT[]));
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

DROP TRIGGER IF EXISTS khipu_episodes_forgotten_insert ON episodes;
CREATE TRIGGER khipu_episodes_forgotten_insert
    BEFORE INSERT ON episodes
    FOR EACH ROW EXECUTE FUNCTION khipu_episodes_forgotten_insert();

DROP TRIGGER IF EXISTS khipu_episodes_forgotten_update ON episodes;
CREATE TRIGGER khipu_episodes_forgotten_update
    BEFORE UPDATE ON episodes
    FOR EACH ROW EXECUTE FUNCTION khipu_episodes_forgotten_update();

DROP TRIGGER IF EXISTS khipu_episode_sources_forgotten ON episode_sources;
CREATE TRIGGER khipu_episode_sources_forgotten
    BEFORE INSERT ON episode_sources
    FOR EACH ROW EXECUTE FUNCTION khipu_episode_sources_forgotten();

-- Backfill, after the triggers and under the locks above: every identity of
-- an episode forgotten before this migration. Its source rows' keys and
-- payload hashes; its natural key from its own columns, so an episode whose
-- source row was never recorded is still covered; and each merge receipt's
-- payload hash and natural key, for a merge whose source row is missing.
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

INSERT INTO forgotten_sources (identity, episode_id)
SELECT k, id FROM (
    SELECT e.id, khipu_natural_source_key(
        coalesce(e.harness, e.raw->>'harness'),
        e.session_id,
        coalesce(e.transcript_range, e.raw->>'transcript_range'),
        coalesce(e.window_id, e.raw->>'window_id'),
        e.raw->>'part') AS k
    FROM episodes e
    WHERE e.deleted_at IS NOT NULL
) natural_keys
WHERE k IS NOT NULL
ON CONFLICT (identity) DO NOTHING;

INSERT INTO forgotten_sources (identity, episode_id)
SELECT k, id FROM (
    SELECT e.id, unnest(ARRAY[
        nullif(m.entry->>'source_id', ''),
        khipu_natural_source_key(
            m.entry->'payload'->>'harness',
            coalesce(m.entry->'payload'->>'session_id', m.entry->>'session_id'),
            m.entry->'payload'->>'transcript_range',
            m.entry->'payload'->>'window_id',
            m.entry->'payload'->>'part')]) AS k
    FROM episodes e
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(e.raw->'merged_from') = 'array'
             THEN e.raw->'merged_from' ELSE '[]'::jsonb END) AS m(entry)
    WHERE e.deleted_at IS NOT NULL AND jsonb_typeof(m.entry) = 'object'
) receipt_keys
WHERE k IS NOT NULL
ON CONFLICT (identity) DO NOTHING;

INSERT INTO schema_migrations (version, note)
VALUES (
    '0030_forgotten_sources',
    'Durable forget: forgotten_sources (identity PK, episode_id, forgotten_at), backfilled from forgotten episodes'' source rows, natural keys and merge receipts; triggers refuse inserting a forgotten source into episodes (natural key or the writer''s named identities) or episode_sources, merging one into an episode, and un-forgetting an episode'
)
ON CONFLICT (version) DO NOTHING;
