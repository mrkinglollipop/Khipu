-- Khipu capture that cannot lose — K3 no-tail-clip (Phase 2, 2026-09-14).
--
-- A window over MAX_TRANSCRIPT is now split on message boundaries into
-- several jobs instead of tail-clipped. window_id groups an episode's
-- siblings so dedup can exempt them from the 5-minute similarity merge —
-- they are DIFFERENT parts of one conversation, not near-duplicates of it.
-- truncated_chars records exactly how much was dropped on the rare window
-- that still overflows the part-count ceiling even after splitting (0 the
-- rest of the time) — loss is recorded, never silent.

ALTER TABLE episodes ADD COLUMN IF NOT EXISTS truncated_chars integer NOT NULL DEFAULT 0;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS window_id text;

CREATE INDEX IF NOT EXISTS idx_episodes_window_id ON episodes (window_id) WHERE window_id IS NOT NULL;

COMMENT ON COLUMN episodes.truncated_chars IS
    'Chars dropped from this episode''s window after the part-count ceiling forced the oldest parts to be merged and clipped. 0 for a losslessly split or unsplit window.';
COMMENT ON COLUMN episodes.window_id IS
    'Shared by every sibling part of one over-budget capture window; NULL for a window that fit in one part. Dedup excludes same-window_id rows from the similarity merge.';

INSERT INTO schema_migrations (version, note)
VALUES (
    '0016_capture_windows',
    'episodes.truncated_chars + window_id — the no-tail-clip window split (K3)'
)
ON CONFLICT (version) DO NOTHING;
