-- Khipu memory reliability W3.2 — first-class commitments.
--
-- Open loops today live only as free-text strings inside episodes.decisions,
-- immutable and unqueryable (root cause D). This table gives them a
-- lifecycle: opened by an episode's open_loops/decisions, closed by a later
-- closed_loop / decision / explicit "done:" match, or aged into `stale`.

CREATE TABLE IF NOT EXISTS commitments (
    id              BIGSERIAL PRIMARY KEY,
    text            TEXT NOT NULL,
    project         TEXT,
    owner           TEXT,
    kind            TEXT NOT NULL DEFAULT 'followup'
                    CHECK (kind IN ('followup', 'blocker', 'question', 'promise')),
    opened_episode  BIGINT REFERENCES episodes (id),
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    due_after       TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed', 'stale')),
    closed_episode  BIGINT REFERENCES episodes (id),
    closed_at       TIMESTAMPTZ,
    close_reason    TEXT,
    content_hash    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commitments_project_status
    ON commitments (project, status);

CREATE INDEX IF NOT EXISTS idx_commitments_opened_at
    ON commitments (opened_at DESC);

-- Dedup an open_loop against ones already open in the same project (W3.3).
CREATE UNIQUE INDEX IF NOT EXISTS uq_commitments_open_content
    ON commitments (project, content_hash) WHERE status = 'open';

-- Commitments join the embeddable corpus (open_from_episode embeds on the
-- active profile via the existing embed helpers, same as episode/topic/media).
ALTER TABLE memory_embeddings
    DROP CONSTRAINT IF EXISTS memory_embeddings_kind_check;

ALTER TABLE memory_embeddings
    ADD CONSTRAINT memory_embeddings_kind_check
    CHECK (kind IN ('episode', 'topic', 'media', 'commitment'));

INSERT INTO schema_migrations (version, note)
VALUES (
    '0009_commitments',
    'memory reliability W3.2: commitments table (open/closed/stale) + memory_embeddings.kind widened to commitment'
)
ON CONFLICT (version) DO NOTHING;
