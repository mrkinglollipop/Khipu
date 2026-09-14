-- Khipu project aliases (K6, 2026-09-14 "memory that works like magic").
--
-- Two remote URLs or repo-root basenames for the SAME real project (a
-- rename, an org transfer, a second local checkout under a different
-- directory name) otherwise read as two different projects everywhere that
-- groups by project (search, commitments, decisions, the pushed slice).
-- khipu.identity.resolve_repo_root consults this table (only when called
-- with a live cursor — hook callers never pass one, so this never adds a DB
-- round trip to the per-turn sandboxed path) and maps a known alias onto its
-- canonical project.

CREATE TABLE IF NOT EXISTS project_aliases (
    alias      TEXT PRIMARY KEY,
    project    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_project_aliases_project
    ON project_aliases (project);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0021_project_aliases',
    'memory reliability K6: project_aliases table — khipu.identity.resolve_repo_root maps a known alias onto its canonical project'
)
ON CONFLICT (version) DO NOTHING;
