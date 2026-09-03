-- Khipu memory reliability W1.1 — stable episode identity columns.
--
-- scope stays the free-text label it already is (root cause A in
-- docs/plans/2026-09-03-memory-reliability.md); these are the STRUCTURED
-- fields the hook resolves itself (khipu.identity.resolve_repo_root) instead
-- of leaving identity to whatever the model happened to write:
--   harness             claude_code | cursor | codex | aegis (session_capture
--                       already infers this; now persisted per episode)
--   repo_root           main checkout root (worktrees resolve to it)
--   project             git remote owner/repo slug, else repo_root basename
--   parent_session_id   lineage for a dispatched child session (KHIPU_PARENT_
--                       SESSION / a harness-carried parent field, when known)
--   transcript_range    "<offset_before>:<offset_after>" byte range of the
--                       transcript window this episode was extracted from —
--                       the exact-window half of ingest dedup (W1.4a)

ALTER TABLE episodes ADD COLUMN IF NOT EXISTS harness TEXT;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS repo_root TEXT;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS project TEXT;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS parent_session_id TEXT;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS transcript_range TEXT;

CREATE INDEX IF NOT EXISTS idx_episodes_project ON episodes (project);
CREATE INDEX IF NOT EXISTS idx_episodes_project_ts ON episodes (project, ts DESC);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0008_identity',
    'memory reliability W1.1: episodes.harness/repo_root/project/parent_session_id/transcript_range + (project) and (project, ts) indexes'
)
ON CONFLICT (version) DO NOTHING;
