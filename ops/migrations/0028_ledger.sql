-- The ledger: what work is done and what is not, per project (Work view,
-- phase 1; docs/plans/2026-10-15-work-view.md, Part A). khipu.ledger is the
-- only writer. Additive and safe to re-run: every object is IF NOT EXISTS.
--
-- Rows are never deleted except by the purge of a row a writer removed at
-- least `ledger_purge_after` ago (or at once, by the owner in the app); the
-- purge leaves the bare marker in ledger_removed so the id never comes back.

CREATE TABLE IF NOT EXISTS ledger_projects (
    project     TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    hidden      BOOLEAN NOT NULL DEFAULT false,
    -- The next `seq` for a row added to this project. Taken under this row's
    -- lock, so rows keep the order they were added in.
    next_seq    BIGINT NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Each project's own state and kind words (compatibility rule 7). A state
-- word maps to one of four meanings the board needs: open, done, dropped,
-- needs (waiting on the owner). Kinds carry no meaning. Words are stored as
-- written and never rewritten in a row.
CREATE TABLE IF NOT EXISTS ledger_vocab (
    project     TEXT NOT NULL REFERENCES ledger_projects (project),
    field       TEXT NOT NULL CHECK (field IN ('state', 'kind')),
    word        TEXT NOT NULL,
    meaning     TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL,
    PRIMARY KEY (project, field, word)
);

CREATE TABLE IF NOT EXISTS ledger_rows (
    project       TEXT NOT NULL REFERENCES ledger_projects (project),
    row_id        TEXT NOT NULL,
    title         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    parent        TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL,
    owner         TEXT NOT NULL DEFAULT 'none',
    evidence      TEXT NOT NULL DEFAULT '',
    link          TEXT NOT NULL DEFAULT '',
    refs          JSONB NOT NULL DEFAULT '{}'::jsonb,
    seq           BIGINT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    version       INTEGER NOT NULL DEFAULT 1,
    removed_at    TIMESTAMPTZ,
    removed_by    JSONB,
    removed_note  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project, row_id)
);

CREATE INDEX IF NOT EXISTS idx_ledger_rows_order
    ON ledger_rows (project, kind, parent, seq);

CREATE INDEX IF NOT EXISTS idx_ledger_rows_removed
    ON ledger_rows (removed_at) WHERE removed_at IS NOT NULL;

-- History is append-only. Two shapes are kept verbatim (compatibility rule 9):
-- a transition {from, to, note, ts} and an event {at, event}. `at_text` keeps
-- the time exactly as the source wrote it; `raw` keeps an imported entry as
-- it was found. `author` is set by Khipu, never taken from the caller.
CREATE TABLE IF NOT EXISTS ledger_history (
    id          BIGSERIAL PRIMARY KEY,
    project     TEXT NOT NULL,
    row_id      TEXT NOT NULL,
    at          TIMESTAMPTZ NOT NULL,
    at_text     TEXT NOT NULL,
    author      JSONB NOT NULL,
    shape       TEXT NOT NULL DEFAULT 'transition' CHECK (shape IN ('transition', 'event')),
    from_state  TEXT,
    to_state    TEXT,
    changes     JSONB NOT NULL DEFAULT '{}'::jsonb,
    note        TEXT NOT NULL DEFAULT '',
    event       TEXT,
    raw         JSONB,
    op_id       TEXT,
    source      TEXT NOT NULL CHECK (source IN ('cli', 'mcp', 'app', 'import', 'nightly'))
);

CREATE INDEX IF NOT EXISTS idx_ledger_history_row
    ON ledger_history (project, row_id, id);

CREATE INDEX IF NOT EXISTS idx_ledger_history_op
    ON ledger_history (op_id) WHERE op_id IS NOT NULL;

-- One row per write that landed, keyed by the caller's op id: a retried write
-- (a timeout, an outbox drain after a crash) finds its op here and returns
-- the first result instead of writing twice.
CREATE TABLE IF NOT EXISTS ledger_ops (
    op_id       TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    row_id      TEXT NOT NULL,
    verb        TEXT NOT NULL,
    result      JSONB NOT NULL DEFAULT '{}'::jsonb,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The bare marker a purged row leaves: who removed it and when. An add, an
-- import or a retried write of this id is refused.
CREATE TABLE IF NOT EXISTS ledger_removed (
    project     TEXT NOT NULL,
    row_id      TEXT NOT NULL,
    removed_by  JSONB,
    removed_at  TIMESTAMPTZ,
    purged_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project, row_id)
);

-- One row per import of an old tracker file (phase 2 writes these).
CREATE TABLE IF NOT EXISTS ledger_imports (
    id          BIGSERIAL PRIMARY KEY,
    project     TEXT NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    file_name   TEXT NOT NULL,
    digest      TEXT NOT NULL,
    counts      JSONB NOT NULL DEFAULT '{}'::jsonb,
    result      TEXT NOT NULL
);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0028_ledger',
    'Work view phase 1: the ledger (rows, history in both shapes, per-project words, ops for retries, removed markers, imports)'
)
ON CONFLICT (version) DO NOTHING;
