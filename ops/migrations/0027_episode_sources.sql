-- Capture identity, phase 1: one row per source contribution to an episode.
-- Scope: docs/plans/2026-10-11-capture-identity.md (decisions 1, 2; phase 1).
--
-- Additive only: a new table, two indexes and a backfill. Nothing reads the
-- table yet, and no existing table, column or constraint changes. Safe to run
-- twice: CREATE ... IF NOT EXISTS, every INSERT is ON CONFLICT DO NOTHING, and
-- the episode-key pass only fills episodes that still have no origin row.
-- Reverse with DROP TABLE episode_sources plus deleting the 0027 row from
-- schema_migrations; no other data depends on it.
--
-- source_key canonical forms (khipu/capture_identity.py is the one Python
-- definition; a test pins this SQL and that module to the same hashes):
--   natural: sha256("khipu-source-v1" US harness US session_id US
--            transcript_range US window_id US part), US = chr(31), NULL = ''
--            used when session_id and transcript_range are both non-empty
--   payload: sha256 of the payload's sorted compact JSON (the merge receipt,
--            raw.merged_from[].source_id)
--   episode: sha256("khipu-episode-v1" US episode id), for historical rows
--            whose payload cannot be reproduced here: origin rows with no
--            natural key (their raw was rewritten after the write, so its
--            JSON no longer hashes to the payload), and an episode whose
--            natural key an older episode already holds
-- payload_sha256 is NULL on backfilled origin rows: not known, not "empty".
--
-- Origin rows take ts from the episodes.ts column, which no writer updates
-- after insert (merges and `khipu episode edit` change summary only); raw.ts
-- is the same instant as text and can hold a string that does not cast.
-- summary_md5 is the summary as first written: raw.summary when it is a
-- string (merge and the original insert both keep it there), else the column.
--
-- The SHARE ROW EXCLUSIVE lock holds episode inserts and merges until this
-- transaction commits, so no capture lands between the backfill and the
-- table becoming visible; writers wait rather than fail, and they probe for
-- the table after their own episode write, so they see it once they resume.

LOCK TABLE episodes IN SHARE ROW EXCLUSIVE MODE;

CREATE TABLE IF NOT EXISTS episode_sources (
    source_key        TEXT PRIMARY KEY,
    episode_id        BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    role              TEXT NOT NULL CHECK (role IN ('origin', 'merged')),
    harness           TEXT,
    session_id        TEXT,
    transcript_range  TEXT,
    window_id         TEXT,
    part              TEXT,
    ts                TIMESTAMPTZ,
    summary_md5       TEXT,
    payload_sha256    TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_episode_sources_ts_summary
    ON episode_sources (ts, summary_md5);
CREATE INDEX IF NOT EXISTS idx_episode_sources_episode
    ON episode_sources (episode_id);

-- 1. Origin rows with a natural key, oldest episode first: when two live
--    episodes share one, the older keeps it and the newer gets an episode key
--    in step 2.
INSERT INTO episode_sources
    (source_key, episode_id, role, harness, session_id, transcript_range,
     window_id, part, ts, summary_md5, payload_sha256)
SELECT encode(sha256(convert_to(concat_ws(chr(31), 'khipu-source-v1',
           coalesce(s.harness, ''), s.session_id, s.transcript_range,
           coalesce(s.window_id, ''), coalesce(s.part, '')), 'UTF8')), 'hex'),
       s.id, 'origin', s.harness, s.session_id, s.transcript_range,
       s.window_id, s.part, s.ts, s.summary_md5, NULL
FROM (
    SELECT e.id, e.ts,
           coalesce(e.harness, e.raw->>'harness') AS harness,
           e.session_id,
           coalesce(e.transcript_range, e.raw->>'transcript_range') AS transcript_range,
           coalesce(e.window_id, e.raw->>'window_id') AS window_id,
           e.raw->>'part' AS part,
           md5(CASE WHEN jsonb_typeof(e.raw->'summary') = 'string'
                    THEN e.raw->>'summary' ELSE e.summary END) AS summary_md5
    FROM episodes e
    WHERE e.deleted_at IS NULL
) s
WHERE coalesce(s.session_id, '') <> '' AND coalesce(s.transcript_range, '') <> ''
ORDER BY s.id
ON CONFLICT (source_key) DO NOTHING;

-- 2. Every live episode still without an origin row gets an episode key.
INSERT INTO episode_sources
    (source_key, episode_id, role, harness, session_id, transcript_range,
     window_id, part, ts, summary_md5, payload_sha256)
SELECT encode(sha256(convert_to('khipu-episode-v1' || chr(31) || e.id::text, 'UTF8')), 'hex'),
       e.id, 'origin',
       coalesce(e.harness, e.raw->>'harness'), e.session_id,
       coalesce(e.transcript_range, e.raw->>'transcript_range'),
       coalesce(e.window_id, e.raw->>'window_id'), e.raw->>'part', e.ts,
       md5(CASE WHEN jsonb_typeof(e.raw->'summary') = 'string'
                THEN e.raw->>'summary' ELSE e.summary END),
       NULL
FROM episodes e
WHERE e.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM episode_sources es
                  WHERE es.episode_id = e.id AND es.role = 'origin')
ORDER BY e.id
ON CONFLICT (source_key) DO NOTHING;

-- 3. One merged row per raw.merged_from[] entry of a live episode, keyed from
--    the stored payload. A source already recorded (the same capture merged
--    twice, or recorded as another episode's origin) keeps its first row.
--    A merged payload's ts is whatever the capture sent; one that does not
--    cast is stored as NULL instead of aborting the migration, hence the loop
--    (a DO block needs no TEMP privilege, unlike a helper function).
DO $$
DECLARE
    m RECORD;
    m_ts TIMESTAMPTZ;
BEGIN
    FOR m IN
        SELECT e.id AS episode_id, x.idx,
               x.entry->'payload'->>'harness' AS harness,
               coalesce(x.entry->'payload'->>'session_id', x.entry->>'session_id') AS session_id,
               x.entry->'payload'->>'transcript_range' AS transcript_range,
               x.entry->'payload'->>'window_id' AS window_id,
               x.entry->'payload'->>'part' AS part,
               coalesce(x.entry->'payload'->>'ts', x.entry->>'ts') AS ts,
               CASE WHEN jsonb_typeof(x.entry->'payload'->'summary') = 'string'
                    THEN x.entry->'payload'->>'summary' END AS summary,
               nullif(x.entry->>'source_id', '') AS source_id
        FROM episodes e
        CROSS JOIN LATERAL jsonb_array_elements(e.raw->'merged_from')
            WITH ORDINALITY AS x(entry, idx)
        WHERE e.deleted_at IS NULL
          AND jsonb_typeof(e.raw->'merged_from') = 'array'
          AND jsonb_typeof(x.entry) = 'object'
        ORDER BY e.id, x.idx
    LOOP
        BEGIN
            m_ts := m.ts::timestamptz;
        EXCEPTION WHEN others THEN
            m_ts := NULL;
        END;
        INSERT INTO episode_sources
            (source_key, episode_id, role, harness, session_id, transcript_range,
             window_id, part, ts, summary_md5, payload_sha256)
        VALUES (
            CASE
              WHEN coalesce(m.session_id, '') <> '' AND coalesce(m.transcript_range, '') <> ''
                THEN encode(sha256(convert_to(concat_ws(chr(31), 'khipu-source-v1',
                       coalesce(m.harness, ''), m.session_id, m.transcript_range,
                       coalesce(m.window_id, ''), coalesce(m.part, '')), 'UTF8')), 'hex')
              WHEN m.source_id IS NOT NULL THEN m.source_id
              ELSE encode(sha256(convert_to('khipu-merged-v1' || chr(31) || m.episode_id::text
                                            || chr(31) || m.idx::text, 'UTF8')), 'hex')
            END,
            m.episode_id, 'merged', m.harness, m.session_id, m.transcript_range,
            m.window_id, m.part, m_ts,
            CASE WHEN m.summary IS NOT NULL THEN md5(m.summary) END,
            m.source_id)
        ON CONFLICT (source_key) DO NOTHING;
    END LOOP;
END
$$;

COMMENT ON TABLE episode_sources IS
    'One row per source contribution to an episode (capture identity phase 1). source_key is the immutable identity; ts/summary_md5 are the legacy (ts, md5(summary)) identity as first written and are never updated.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0027_episode_sources',
    'Capture identity phase 1: episode_sources (source_key PK, episode_id, role origin|merged, harness/session_id/transcript_range/window_id/part, ts, summary_md5, payload_sha256) + (ts, summary_md5) and episode_id indexes; backfilled from live episodes and raw.merged_from[]'
)
ON CONFLICT (version) DO NOTHING;
