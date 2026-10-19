-- Project renames, slice B (docs/plans/2026-10-19-repo-renames.md).
--
-- project_aliases gains where each alias came from and what proved it, so an
-- automatic rename can be shown and undone:
--   source    'owner' (khipu project alias add), 'checkout' (the same checkout
--             reported a new name), or 'github' (a later slice)
--   evidence  JSON: the old and new name, the root commit, the checkout path
--
-- project_roots remembers each root commit a project name was seen with, so a
-- later slice can notice two names sharing one. project_pairs holds the
-- "same project?" question for such a pair (a < b): created here for slice D,
-- not read or written by anything yet.
--
-- project_checkouts is the hub's memory of each checkout (the device and inode
-- of its git common dir, so a moved folder keeps its key and a re-cloned one
-- gets a new key): the last project name and kind ('remote', 'folder' or
-- 'unknown') a capture reported for it, and when. The hub, not the hook,
-- compares successive observations to decide a rename.
--
-- Additive and safe to run twice. Reverse with DROP TABLE project_checkouts,
-- project_pairs, project_roots, ALTER TABLE project_aliases DROP COLUMN source, evidence, and
-- deleting the 0039 row from schema_migrations.

ALTER TABLE project_aliases ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'owner';
ALTER TABLE project_aliases ADD COLUMN IF NOT EXISTS evidence JSONB;

CREATE TABLE IF NOT EXISTS project_roots (
    project     TEXT NOT NULL,
    root_commit TEXT NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project, root_commit)
);

CREATE INDEX IF NOT EXISTS idx_project_roots_root_commit ON project_roots (root_commit);

CREATE TABLE IF NOT EXISTS project_checkouts (
    checkout_key TEXT PRIMARY KEY,
    root_commit  TEXT NOT NULL,
    project      TEXT NOT NULL,
    project_kind TEXT NOT NULL,  -- 'remote' | 'folder' | 'unknown'
    last_seen_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS project_pairs (
    a           TEXT NOT NULL,
    b           TEXT NOT NULL,
    root_commit TEXT,
    state       TEXT NOT NULL,  -- 'asked' | 'kept_separate'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ,
    PRIMARY KEY (a, b),
    CHECK (a < b)
);

INSERT INTO schema_migrations (version, note)
VALUES ('0039_project_rename_evidence', 'project_aliases.source/evidence; project_roots; project_checkouts; project_pairs (slice D)')
ON CONFLICT (version) DO NOTHING;
