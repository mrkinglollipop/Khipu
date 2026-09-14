-- Khipu decisions registry (O2, 2026-09-14 "memory that works like magic").
--
-- episodes.decisions is an immutable JSONB array of strings: no date beyond
-- the episode's own ts, no rationale, and no way to say a later decision
-- replaced an earlier one — a reversal ranks beside the original forever
-- (measured live: 36,967 decision strings across 6,815 episodes, no
-- decisions table at all). This table gives each decision string its own
-- row with a lifecycle; khipu.decisions writes/reads it. Additive only —
-- episodes.decisions is untouched and stays the source `khipu decisions
-- backfill` walks.

CREATE TABLE IF NOT EXISTS decisions (
    id              BIGSERIAL PRIMARY KEY,
    project         TEXT,
    text            TEXT NOT NULL,
    rationale       TEXT,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    episode_id      BIGINT REFERENCES episodes (id),
    session_id      TEXT,
    superseded_by   BIGINT REFERENCES decisions (id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decisions_project_decided_at
    ON decisions (project, decided_at DESC);

CREATE INDEX IF NOT EXISTS idx_decisions_episode
    ON decisions (episode_id);

CREATE INDEX IF NOT EXISTS idx_decisions_superseded_by
    ON decisions (superseded_by) WHERE superseded_by IS NOT NULL;

-- khipu.decisions.insert_decisions_from_episode dedups by (project,
-- normalised text, 30-day window) in application code (a plain unique index
-- cannot express the time-window part), so this index only speeds that
-- lookup — it enforces nothing on its own.
CREATE INDEX IF NOT EXISTS idx_decisions_dedup_lookup
    ON decisions (project, decided_at);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0019_decisions',
    'memory reliability O2: decisions table (decided_at, rationale, superseded_by) — episodes.decisions strings get a queryable lifecycle'
)
ON CONFLICT (version) DO NOTHING;
