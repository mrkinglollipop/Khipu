-- Khipu deliverables index (O4, 2026-09-14 "memory that works like magic").
--
-- 2,193 `path:` graph nodes come from topic BODIES today; none from episodes
-- — "have I already built this?" is unanswerable from an episode alone. This
-- table gives files written/created, PR/issue URLs, and release tags their
-- own row per episode, extracted by khipu.extract.extract_deliverables
-- (a regex pass, same tier as the K2 verbatim tier — before the model call,
-- never lost to summarisation).

CREATE TABLE IF NOT EXISTS deliverables (
    id          BIGSERIAL PRIMARY KEY,
    project     TEXT,
    kind        TEXT NOT NULL CHECK (kind IN ('file', 'pr', 'issue', 'release')),
    path        TEXT,
    url         TEXT,
    title       TEXT,
    episode_id  BIGINT REFERENCES episodes (id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_deliverables_project_created_at
    ON deliverables (project, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_deliverables_episode
    ON deliverables (episode_id);

-- khipu.deliverables.insert_deliverables_from_episode dedups by (project,
-- kind, path, url) in application code before every insert; this index only
-- speeds that lookup.
CREATE INDEX IF NOT EXISTS idx_deliverables_dedup_lookup
    ON deliverables (project, kind, path, url);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0020_deliverables',
    'memory reliability O4: deliverables table (file/pr/issue/release) extracted from episode windows — "have I already built this?"'
)
ON CONFLICT (version) DO NOTHING;
