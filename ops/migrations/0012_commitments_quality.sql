-- Khipu commitments quality (2026-09-04).
--
-- Measured on the live hub: 328 open commitments, all opened in the last 7
-- days (~150/day), 290 of them on one project. Zero exact duplicates but the
-- content is dominated by in-flight status ("Drive 46 is still running"),
-- the assistant's own same-session plan steps, inter-agent coordination
-- chatter, and near-restatements of one item across successive captures of
-- the same session. Owed is unusable at that signal-to-noise ratio.
--
-- Three schema changes back the fix:
--   * last_seen_at / seen_count — a paraphrase of an already-open commitment
--     no longer inserts a second row; it touches the first one instead
--     (khipu.commitments.open_from_episode). last_seen_at is also the clock
--     mark_stale ages against, so a commitment that keeps being restated
--     stays open and a genuinely silent one expires.
--   * status 'dropped' — the hygiene re-judge never DELETEs. A rejected
--     commitment is parked in 'dropped' with a close_reason, so a bad verdict
--     is reversible (`khipu owed --reopen ID`).

ALTER TABLE commitments ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS seen_count INTEGER NOT NULL DEFAULT 1;

-- Widen the status CHECK to allow 'dropped' (0009 constrained it to
-- open/closed/stale). Drop-then-add so re-running the migration is a no-op.
ALTER TABLE commitments DROP CONSTRAINT IF EXISTS commitments_status_check;
ALTER TABLE commitments
    ADD CONSTRAINT commitments_status_check
    CHECK (status IN ('open', 'closed', 'stale', 'dropped'));

CREATE INDEX IF NOT EXISTS idx_commitments_last_seen
    ON commitments (last_seen_at DESC) WHERE status = 'open';

COMMENT ON COLUMN commitments.last_seen_at IS
    'Last capture that restated this commitment (open_from_episode near-duplicate hit). NULL = never restated since opened_at; mark_stale ages against COALESCE(last_seen_at, opened_at).';
COMMENT ON COLUMN commitments.seen_count IS
    'How many captures have stated this commitment, including the one that opened it.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0012_commitments_quality',
    'commitments quality: last_seen_at + seen_count (paraphrase dedup at open, silence-based expiry) and status ''dropped'' for the hygiene re-judge'
)
ON CONFLICT (version) DO NOTHING;
