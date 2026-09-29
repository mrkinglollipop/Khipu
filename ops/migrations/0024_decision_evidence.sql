-- Decision evidence and lifecycle (Phase 2, session A).
--
-- B2 in docs/research/hindsight-plan-review-2026-09-28.md: production holds
-- 40,409 decision rows, 0 superseded, 0 with a rationale, because the only
-- writer was `khipu decisions supersede OLD NEW` — a CLI no cloud harness can
-- reach. This migration gives `decisions` the columns an agent-callable
-- write path (khipu_decisions_update) and conservative capture-time
-- detection need: who/what said it, the evidence behind it, and a full
-- supersede/retract lifecycle instead of the single `superseded_by` pointer.
-- `decision_links` holds candidates a detector proposes but nothing has
-- confirmed yet, per "Evidence and changing-fact rules" #7: detection never
-- changes authoritative state on its own authority.
--
-- Additive only. Every new column is nullable or defaulted; khipu.decisions
-- gates every reader/writer on khipu.db.has_columns so this table keeps
-- working, degraded, before this migration ever runs. No unique index is
-- added to `decisions` — production already holds duplicate text.
--
-- NOT applied anywhere by this change. Applied to the hub later, by a
-- person, after authorization.

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS source_kind TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS evidence JSONB;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS supersede_source TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS supersede_reason TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS retracted_at TIMESTAMPTZ;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS retract_reason TEXT;

CREATE TABLE IF NOT EXISTS decision_links (
    id              BIGSERIAL PRIMARY KEY,
    old_id          BIGINT NOT NULL REFERENCES decisions (id) ON DELETE CASCADE,
    new_id          BIGINT NOT NULL REFERENCES decisions (id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    confidence      REAL,
    source          TEXT,
    reason          TEXT,
    state           TEXT NOT NULL DEFAULT 'candidate',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    UNIQUE (old_id, new_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_decision_links_state_created
    ON decision_links (state, created_at DESC);

INSERT INTO schema_migrations (version, note)
VALUES (
    '0024_decision_evidence',
    'Phase 2A: decisions evidence/lifecycle columns (source_kind, evidence, '
    'superseded_at/source/reason, retracted_at/reason) + decision_links '
    '(candidate/applied/rejected/restored supersedes|conflicts links)'
)
ON CONFLICT (version) DO NOTHING;
